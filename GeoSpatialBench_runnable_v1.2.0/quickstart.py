#!/usr/bin/env python3
"""Create an isolated environment and run the bundled benchmark entirely offline."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index-url',help='Optional pip mirror; used only for dependency installation')
    parser.add_argument('--skip-install',action='store_true',help='Use the current Python with dependencies already installed')
    parser.add_argument('--skip-build',action='store_true')
    parser.add_argument('--out',type=Path)
    parser.add_argument('--per-relation',type=int,default=1)
    args = parser.parse_args()
    if sys.version_info<(3,10):
        parser.error('Python 3.10 or newer is required; Python 3.13 is the verified environment')
    runtime = ROOT/'.runtime'
    (runtime/'tmp').mkdir(parents=True,exist_ok=True)
    (runtime/'matplotlib').mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(TEMP=str(runtime/'tmp'),TMP=str(runtime/'tmp'),MPLCONFIGDIR=str(runtime/'matplotlib'),PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
    python = Path(sys.executable)
    if not args.skip_install:
        venv = ROOT/'.venv'
        python = venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        if not python.exists():
            subprocess.run([sys.executable,'-m','venv',str(venv)],env=env,check=True)
        requirements = ROOT/('requirements-lock-py313.txt' if sys.version_info[:2]==(3,13) else 'requirements.txt')
        command = [str(python),'-m','pip','install','--timeout','120','-r',str(requirements)]
        if args.index_url:
            command += ['--index-url',args.index_url]
        subprocess.run(command,cwd=ROOT,env=env,check=True)
        subprocess.run([str(python),'-m','pip','check'],cwd=ROOT,env=env,check=True)
    command = [str(python),str(ROOT/'scripts/benchmark.py'),'quickstart','--per-relation',str(args.per_relation)]
    if args.skip_build:
        command.append('--skip-build')
    if args.out:
        command += ['--out',str(args.out.resolve())]
    subprocess.run(command,cwd=ROOT,env=env,check=True)
    print('PASS. Open outputs/latest_quickstart.json for the report location. No model API was called.')
    return 0

if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (OSError,subprocess.CalledProcessError) as exc:
        print(f'Quickstart failed: {exc}',file=sys.stderr)
        raise SystemExit(1)
