"""Rebuild the shipped memory module using the locally installed pinned SDK."""
from pathlib import Path
import hashlib
import json
import re
import subprocess


def build(root=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    module = root / 'memory_modules' / 'spacetimedb'
    expected = json.loads((module / 'package.json').read_text())['dependencies']['spacetimedb']
    installed = json.loads((module / 'node_modules/spacetimedb/package.json').read_text())['version']
    if installed != expected:
        raise ValueError(f'Expected SpacetimeDB SDK {expected}; installed {installed}.')
    compiler = json.loads((module / 'node_modules/typescript/package.json').read_text())['version']
    compiler_expected = json.loads((module / 'package.json').read_text())['devDependencies']['typescript']
    if compiler != compiler_expected:
        raise ValueError(f'Expected TypeScript {compiler_expected}; installed {compiler}.')
    cli = subprocess.check_output(['spacetime', '--version'], text=True)
    if f'tool version {expected};' not in cli:
        raise ValueError(f'Expected SpacetimeDB CLI {expected}.')
    subprocess.run(['spacetime', 'build', '--module-path', str(module)], check=True)
    bundle = (module / 'dist/bundle.js').read_text()
    # Bundler region comments otherwise expose dependency checkout locations and
    # make equivalent builds vary with the developer's directory layout.
    bundle = re.sub(r'(?m)^//#region .*?/node_modules/', '//#region node_modules/', bundle)
    (module / 'module.js').write_text(bundle)
    inputs = {name: hashlib.sha256((module / name).read_bytes()).hexdigest()
              for name in ('src/index.ts', 'package.json', 'tsconfig.json')}
    receipt = {'protocol': 1, 'sdk': expected, 'sha256': hashlib.sha256(bundle.encode()).hexdigest(), 'inputs': inputs}
    (module / 'module.manifest.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


if __name__ == '__main__':
    print(json.dumps(build()))
