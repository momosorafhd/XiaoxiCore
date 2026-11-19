"""Isolated local ONNX embedding subprocess; stdin/stdout JSON, no downloads."""
import json
from pathlib import Path
import sys

from library_v04_encoder import LocalBGE


def main():
    request=json.load(sys.stdin)
    if not isinstance(request,dict) or set(request)!={'modelDir','operation','text'}:
        raise ValueError('invalid embedding request')
    if not isinstance(request['text'],str) or not request['text'] or len(request['text'])>1005000:
        raise ValueError('invalid embedding text')
    model=LocalBGE(request['modelDir'],site_dir=str(Path(sys.prefix)/'lib'/('python'+str(sys.version_info.major)+'.'+str(sys.version_info.minor))/'site-packages'))
    if request['operation']=='query':result=model.encode_query(request['text'])
    elif request['operation']=='document':result=model.encode_document(request['text'])
    else:raise ValueError('invalid embedding operation')
    print(json.dumps(result,allow_nan=False))


if __name__=='__main__':main()
