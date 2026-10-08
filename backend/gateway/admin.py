import argparse
import json
from pathlib import Path
from .core import GatewayCore

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',required=True)
    parser.add_argument('--secret-file',required=True)
    parser.add_argument('--config-file',required=True)
    parser.add_argument('operation',choices=['pair','costs'])
    args=parser.parse_args()
    core=GatewayCore(args.database,Path(args.secret_file).read_bytes(),json.loads(Path(args.config_file).read_text()))
    print(core.pairing_code() if args.operation=='pair' else json.dumps(core.costs()))

if __name__=='__main__':main()
