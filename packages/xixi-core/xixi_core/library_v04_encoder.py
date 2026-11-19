"""CPU-only local BGE encoder. Runtime never downloads or opens network sockets."""
import hashlib
import json
from pathlib import Path
import sys
import threading


class LocalBGE:
    def __init__(self, model_dir, site_dir):
        self.model_dir=Path(model_dir)
        self.site_dir=site_dir
        manifest=json.loads((self.model_dir/'manifest.json').read_text())
        for name,digest in manifest['files'].items():
            path=self.model_dir/name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise ValueError('model_integrity_failed')
        self.space='bge-small-zh-v1.5:Qdrant:'+manifest['revision']+':cls:normalize:prefix:tokens450overlap32'
        self.session=None
        self.lock=threading.RLock()

    def _load(self):
        if self.session is not None: return
        if self.site_dir not in sys.path: sys.path.append(self.site_dir)
        import numpy as np
        import onnxruntime as ort
        ort.disable_telemetry_events()
        from tokenizers import Tokenizer
        self.np=np
        self.tokenizer=Tokenizer.from_file(str(self.model_dir/'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=512)
        self.full_tokenizer=Tokenizer.from_file(str(self.model_dir/'tokenizer.json'))
        self.full_tokenizer.no_truncation()
        options=ort.SessionOptions()
        options.intra_op_num_threads=2
        options.inter_op_num_threads=1
        options.log_severity_level=3
        self.session=ort.InferenceSession(str(self.model_dir/'model_optimized.onnx'),sess_options=options,providers=['CPUExecutionProvider'])

    def _encode(self,text):
        self._load()
        encoding=self.tokenizer.encode(text)
        np=self.np
        values={'input_ids':np.array([encoding.ids],dtype=np.int64),
                'attention_mask':np.array([encoding.attention_mask],dtype=np.int64),
                'token_type_ids':np.array([encoding.type_ids],dtype=np.int64)}
        output=self.session.run(None,{i.name:values[i.name] for i in self.session.get_inputs()})[0]
        vec=output[0,0] if output.ndim==3 else output[0]
        norm=float(np.linalg.norm(vec))
        if not norm or len(vec)!=512 or not np.isfinite(vec).all(): raise ValueError('invalid_embedding')
        return [float(x) for x in vec/norm]

    def encode_query(self,text):
        with self.lock:
            return self._encode('为这个句子生成表示以用于检索相关文章：'+text)

    def encode_document(self,text):
        with self.lock:
            self._load()
            offsets=self.full_tokenizer.encode(text,add_special_tokens=False).offsets
            chunks=[]
            for start_token in range(0,len(offsets),418):
                part=offsets[start_token:start_token+450]
                if not part: break
                start=part[0][0]; end=part[-1][1]
                if start_token==0: start=0
                if start_token+450>=len(offsets): end=len(text)
                chunks.append((start,end,self._encode(text[start:end])))
                if end==len(text): break
            return chunks or [(0,len(text),self._encode(text))]
