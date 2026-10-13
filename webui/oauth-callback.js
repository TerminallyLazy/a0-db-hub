import { fetchApi } from '/js/api.js';
const params = new URLSearchParams(location.search);
history.replaceState(null, '', location.pathname);
try {
  const response = await fetchApi('/plugins/db_hub/oauth', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'finish',state:params.get('state'),code:params.get('code'),error:params.get('error')})});
  const result = await response.json();
  document.getElementById('status').textContent = result.ok ? 'Authorization complete. Return to Database Hub and click Finish sign-in.' : result.error;
} catch { document.getElementById('status').textContent = 'Authorization could not complete. Return to Database Hub and start again.'; }
