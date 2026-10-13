import base64
import hashlib
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

from usr.plugins.db_hub.helpers import oauth, workspace, registry
from usr.plugins.db_hub.helpers.base import DbHubError
from usr.plugins.db_hub.api.oauth import OAuth
from usr.plugins.db_hub.api.workspace import Workspace


class OAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        oauth._FLOWS.clear()
        self.addCleanup(oauth._FLOWS.clear)

    async def start(self, provider='bigquery', **kwargs):
        return await oauth.start('owner', ('project','profile'), 'http://localhost:49805', {'provider':provider,'client_id':'client','client_secret':'client-secret',**kwargs})

    async def test_pkce_state_scope_callback_and_single_use(self):
        result = await self.start()
        handle = result['handle']; params=parse_qs(urlsplit(result['authorization_url']).query)
        verifier=oauth._FLOWS[handle]['verifier']
        self.assertEqual(params['code_challenge'],[base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')])
        self.assertEqual(params['redirect_uri'],['http://localhost:49805'+oauth.CALLBACK])
        self.assertNotIn('client-secret', json.dumps(result))
        self.assertNotIn(verifier, json.dumps(result))
        for owner,scope in [('stranger',('project','profile')),('owner',('another','profile'))]:
            with self.assertRaises(DbHubError): await oauth.status(owner,handle,scope)
        with patch.object(oauth,'_token_request',return_value={'access_token':'secret-access','refresh_token':'secret-refresh','expires_in':3600}) as exchange:
            self.assertEqual(await oauth.finish('owner',{'state':handle,'code':'code'}),{'complete':True})
            self.assertEqual(exchange.call_args.args[-1]['code_verifier'],verifier)
            with self.assertRaises(DbHubError): await oauth.finish('owner',{'state':handle,'code':'code'})
        self.assertEqual((await oauth.status('owner',handle,('project','profile')))['status'],'complete')

    async def test_denial_and_expiry_never_keep_client_secret(self):
        handle=(await self.start())['handle']
        with self.assertRaises(DbHubError): await oauth.finish('owner',{'state':handle,'error':'access_denied'})
        self.assertNotIn('client_secret',oauth._FLOWS[handle])
        oauth._FLOWS[handle]['expires']=time.monotonic()-1
        with self.assertRaises(DbHubError): await oauth.status('owner',handle,('project','profile'))

    async def test_import_only_secret_reference_and_refresh_rotation(self):
        handle=(await self.start())['handle']
        state=oauth._FLOWS[handle]; state.update(status='complete',tokens={'access_token':'old-access','refresh_token':'old-refresh','expires_at':time.time()-1})
        with patch.object(oauth.extract,'get_default_secrets_manager',return_value=Mock()), patch.object(oauth.extract,'_validate_new_key',side_effect=lambda key,manager:key), patch.object(oauth.extract,'_write_secret') as save:
            result=oauth.import_draft('owner',handle,('project','profile'),{'dsn':'bigquery://project/data','connection_id':'bq'})
            encoded=save.call_args.args[2]
        self.assertNotIn('old-access',json.dumps(result)); self.assertTrue(result['connection']['readonly'])
        self.assertNotIn('tokens',state)
        reference=result['connection']['options']['oauth_credential']
        with patch.object(oauth.secrets_bridge,'resolve_placeholders',return_value=encoded), patch.object(oauth,'_token_request',return_value={'access_token':'new-access','refresh_token':'new-refresh','expires_in':3600}), patch.object(oauth.extract,'get_default_secrets_manager',return_value=Mock()),patch.object(oauth.extract,'_write_secret') as save:
            options=oauth.resolve_options('bigquery',{},reference)
            self.assertEqual(options['oauth_access_token'],'new-access')
            stored=json.loads(base64.urlsafe_b64decode(save.call_args.args[2])); self.assertEqual(stored['tokens']['refresh_token'],'new-refresh')
            with self.assertRaises(DbHubError): oauth.resolve_options('snowflake',{},reference)

    async def test_supabase_discovery_only_and_no_deprecated_scope(self):
        result=await self.start('supabase'); self.assertNotIn('scope=',result['authorization_url'])
        state=oauth._FLOWS[result['handle']];state.update(status='complete',tokens={'access_token':'account-token'})
        with patch.object(oauth.providers,'connect',AsyncMock(return_value={'handle':'discovery','projects':[]})) as connect:
            result2=await oauth.discovery('owner',result['handle'],('project','profile'))
            self.assertEqual(result2['handle'],'discovery'); self.assertEqual(connect.call_args.args[2],'account-token')
        with self.assertRaises(DbHubError): oauth.import_draft('owner',result['handle'],('project','profile'),{})

    async def test_custom_endpoints_reject_private_hosts_and_non_https(self):
        with self.assertRaises(DbHubError): await self.start('snowflake',account_url='https://attacker.example')
        with patch.object(oauth.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(DbHubError): await self.start('neo4j',authorization_url='https://identity.example/authorize',token_url='https://identity.example/token')
        with self.assertRaises(DbHubError): await self.start('neo4j',authorization_url='http://identity.example',token_url='https://identity.example')

    def test_protected_api_endpoints_and_driver_redaction(self):
        for handler in (OAuth,Workspace):
            self.assertTrue(handler.requires_auth()); self.assertTrue(handler.requires_csrf())
        bound=registry.BoundConnection(None,{}, {}, ('',''),sensitive_values=('synthetic-access',))
        with patch.object(registry.secrets_bridge,'mask_text',side_effect=lambda value,**kw:value):
            self.assertEqual(bound.mask('token synthetic-access failed'),'token *** failed')


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bound=Mock()
        self.bound.conn_cfg={'id':'test','driver':'postgres','readonly':False,'options':{}}
        self.bound.glob={'max_rows':200}; self.bound._timeout.return_value=30
        self.bound.mask.side_effect=lambda text:text
        self.bound.schema=AsyncMock(return_value={'tables':[{'table_schema':'public','table_name':'some"table'}]})
        self.bound.query=AsyncMock(return_value={'rows':[{'ok':1}]})
        self.bound.driver.is_read_statement.side_effect=lambda value:value.startswith('SELECT')
        for target,value in [(workspace,'enabled_plugins')]:
            patcher=patch.object(target,value,return_value=['db_hub','_memory']); patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(workspace.registry,'get_connection',AsyncMock(return_value=self.bound));self.get=patcher.start();self.addCleanup(patcher.stop)

    async def test_saved_scope_schema_and_quoted_preview(self):
        result=await workspace.execute({'action':'schema','connection':'test'},'proj','profile')
        self.assertEqual(self.get.call_args.kwargs,{'project_name':'proj','agent_profile':'profile'})
        self.assertEqual(result['resources'][0]['name'],'public.some"table')
        await workspace.execute({'action':'preview','connection':'test','target':'public.some"table','limit':50})
        self.bound.query.assert_awaited_with('SELECT * FROM "public"."some""table" LIMIT 50 OFFSET 0',readonly=True)
        with self.assertRaises(DbHubError): await workspace.execute({'action':'preview','target':'invented; DROP TABLE x'})

    async def test_readonly_and_confirmed_writes_respect_saved_access(self):
        await workspace.execute({'action':'query','statement':'SELECT 1'})
        self.assertTrue(self.bound.query.call_args.kwargs['readonly'])
        for data in [{'statement':'DELETE FROM x'}, {'statement':'DELETE FROM x','allow_writes':True}, {'statement':'SELECT function_that_writes()','allow_writes':True}]:
            with self.assertRaises(DbHubError): await workspace.execute({'action':'query',**data})
        await workspace.execute({'action':'query','statement':'DELETE FROM x','allow_writes':True,'confirmed':True})
        self.assertFalse(self.bound.query.call_args.kwargs['readonly'])
        self.bound.conn_cfg['readonly']=True
        with self.assertRaises(DbHubError): await workspace.execute({'action':'query','statement':'DELETE FROM x','allow_writes':True,'confirmed':True})

    async def test_multiple_statements_invalid_params_and_disabled_scope(self):
        for statement in ['SELECT 1; DELETE FROM x','', 'SELECT 1;;DELETE FROM x']:
            with self.assertRaises(DbHubError): await workspace.execute({'action':'query','statement':statement})
        with self.assertRaises(DbHubError): await workspace.execute({'action':'query','statement':'SELECT 1','params':[]})
        with patch.object(workspace,'enabled_plugins',return_value=[]):
            with self.assertRaises(DbHubError): await workspace.execute({'action':'schema'})
        self.bound.query.assert_not_called()

    def test_memory_summary_does_not_disclose_dsns_or_secrets(self):
        cfg={'memory_backend':{'enabled':True,'connections':[{'connection':'pg'}]},'connections':[{'id':'pg','driver':'postgres','readonly':False,'description':'test','enabled':True,'dsn':'secret'}]}
        with patch.object(workspace.registry,'load_config',return_value=cfg):
            summary=workspace.summary();self.assertTrue(summary['memory']['external']);self.assertTrue(summary['connections'][0]['memory']);self.assertNotIn('secret',json.dumps(summary))
            cfg['memory_backend']['enabled']=False
            self.assertFalse(workspace.summary()['memory']['external'])

class DriverConnectionTests(unittest.IsolatedAsyncioTestCase):
    def test_snowflake_oauth_uses_connector_token_auth(self):
        from usr.plugins.db_hub.helpers.drivers.snowflake import _parse_dsn
        params=_parse_dsn('snowflake://org-account/warehouse/db/schema',{'oauth_access_token':'synthetic'})
        self.assertEqual(params['authenticator'],'oauth');self.assertEqual(params['token'],'synthetic');self.assertNotIn('password',params)

    async def test_convex_uses_application_token_authorization(self):
        from usr.plugins.db_hub.helpers.drivers.convex import ConvexDriver
        driver=ConvexDriver()
        await driver.connect({'oauth_access_token':'synthetic'},'https://test.convex.cloud')
        self.assertEqual(driver._client.headers['Authorization'],'Convex synthetic')
        await driver.close()

    def test_mongo_output_pipeline_is_a_write(self):
        from usr.plugins.db_hub.helpers.drivers.mongodb import MongoDriver
        driver=MongoDriver()
        self.assertTrue(driver.api_is_write({'op':'aggregate','pipeline':[{'$match':{}},{'$out':'copied'}]},{}))
        self.assertTrue(driver.api_is_write({'op':'aggregate','pipeline':[{'$merge':'copied'}]},{}))
        self.assertFalse(driver.api_is_write({'op':'aggregate','pipeline':[{'$match':{}}]},{}))

    async def test_qdrant_preview_preserves_integer_pagination_cursor(self):
        from usr.plugins.db_hub.helpers.drivers.qdrant import QdrantDriver
        driver=QdrantDriver();driver._client=Mock();driver._run=AsyncMock(return_value=([SimpleNamespace(id=1,payload={'text':'synthetic'})],42))
        result=await driver.browse('collection',5,12)
        self.assertEqual(result['next_cursor'],42);self.assertEqual(driver._run.call_args.kwargs['offset'],12)
        self.assertFalse(driver._run.call_args.kwargs['with_vectors'])

    async def test_postgres_readonly_is_enforced_by_transaction(self):
        from usr.plugins.db_hub.helpers.drivers.postgres import PostgresDriver
        driver=PostgresDriver();conn=Mock();conn.fetch=AsyncMock(return_value=[{'ok':1}]);ctx=AsyncMock();ctx.__aenter__.return_value=conn
        driver._pool=Mock();driver._pool.acquire.return_value=ctx
        conn.transaction.return_value=AsyncMock()
        result=await driver.query('SELECT 1',{},True)
        conn.transaction.assert_called_once_with(readonly=True);self.assertEqual(result['rows'],[{'ok':1}])
