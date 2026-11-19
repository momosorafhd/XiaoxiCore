"""Independent authenticated reverse OneBot WS. No existing adapter/config edits."""
import base64
import hashlib
import hmac
import ipaddress
import json
import select
import socket
import struct
import time

class IdleTimeout(TimeoutError):
    """The reverse listener received no frame during this poll."""


_PRIVATE=tuple(ipaddress.ip_network(x) for x in ('127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
def private_ipv4(value):
    try: address=ipaddress.IPv4Address(value)
    except (ValueError,TypeError):raise ValueError('Explicit private IPv4 required')
    if not any(address in network for network in _PRIVATE):raise ValueError('Explicit private IPv4 required')
    return str(address)

class ReverseConnection:
    MAX=2*1024*1024
    def __init__(self,sock):self.sock=sock;sock.settimeout(10)
    def _exact(self,n):
        value=b''
        while len(value)<n:
            chunk=self.sock.recv(n-len(value))
            if not chunk:raise ConnectionError('Reverse WS closed')
            value+=chunk
        return value
    def _send(self,op,payload):
        n=len(payload)
        if n>self.MAX:raise ValueError('Reverse WS frame too large')
        header=bytes([128|op,n if n<126 else 126 if n<65536 else 127])
        if n>=126:header+=struct.pack('!H' if n<65536 else '!Q',n)
        self.sock.sendall(header+payload)
    def send(self,value):self._send(1,json.dumps(value,ensure_ascii=False,allow_nan=False).encode())
    def recv(self,timeout=1):
        if not select.select([self.sock],[],[],timeout)[0]:raise IdleTimeout()
        result=b'';started=False
        while True:
            a,b=self._exact(2);fin=bool(a&128);op=a&15;n=b&127
            if a&112 or not b&128:raise ValueError('Masked reverse client frame required')
            if n==126:n=struct.unpack('!H',self._exact(2))[0]
            elif n==127:n=struct.unpack('!Q',self._exact(8))[0]
            if n>self.MAX or len(result)+n>self.MAX:raise ValueError('Reverse WS frame too large')
            mask=self._exact(4);data=bytes(c^mask[i%4] for i,c in enumerate(self._exact(n)))
            if op>=8:
                if not fin or n>125:raise ValueError('Invalid reverse control frame')
                if op==8:raise ConnectionError('Reverse WS peer closed')
                if op==9:self._send(10,data)
                elif op!=10:raise ValueError('Invalid reverse control opcode')
                continue
            if op==1 and not started:started=True
            elif op!=0 or not started:raise ValueError('Reverse WS only accepts text')
            result+=data
            if fin:return json.loads(result.decode('utf-8'))
    def close(self):
        try:self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.sock.close()

class ReverseListener:
    PATH='/xixi-onebot'
    def __init__(self,config):
        self.host=private_ipv4(config.get('listenHost'));self.peer=private_ipv4(config.get('allowedPeerIp'))
        self.port=config.get('listenPort');self.bot=config.get('botId');self.token=config.get('reverseToken')
        if not isinstance(self.port,int) or isinstance(self.port,bool) or not 1024<=self.port<=65535:raise ValueError('Explicit unprivileged reverse port required')
        if not isinstance(self.bot,str) or not self.bot.isdigit():raise ValueError('Exact reverse bot binding required')
        if not isinstance(self.token,str) or not 32<=len(self.token)<=4096 or not self.token.isascii() or any(not 33<=ord(c)<=126 for c in self.token):raise ValueError('New private reverse token required')
        self.server=None
    def open(self):
        server=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);server.bind((self.host,self.port));server.listen(1);server.settimeout(.5)
        except BaseException:server.close();raise
        self.server=server
    def accept(self):
        try:sock,peer=self.server.accept()
        except socket.timeout:raise IdleTimeout()
        sock.settimeout(3)
        try:
            if peer[0]!=self.peer:raise PermissionError('Reverse peer mismatch')
            raw=b''
            while not raw.endswith(b'\r\n\r\n'):
                chunk=sock.recv(1)
                if not chunk:raise ConnectionError('Reverse handshake closed')
                raw+=chunk
                if len(raw)>16384:raise ValueError('Reverse handshake too large')
            lines=raw.decode('ascii').split('\r\n');headers={}
            if lines[0]!='GET '+self.PATH+' HTTP/1.1':raise PermissionError('Reverse path mismatch')
            for line in lines[1:]:
                if not line:continue
                name,value=line.split(':',1);name=name.lower()
                if name in headers:raise ValueError('Duplicate reverse header')
                headers[name]=value.strip()
            if not hmac.compare_digest(headers.get('authorization',''),'Bearer '+self.token):raise PermissionError('Reverse auth mismatch')
            if headers.get('x-self-id')!=self.bot or headers.get('x-client-role')!='Universal':raise PermissionError('Reverse bot or role mismatch')
            if headers.get('upgrade','').lower()!='websocket' or 'upgrade' not in [x.strip().lower() for x in headers.get('connection','').split(',')] or headers.get('sec-websocket-version')!='13':raise ValueError('Reverse upgrade malformed')
            key=headers.get('sec-websocket-key','')
            if len(base64.b64decode(key,validate=True))!=16:raise ValueError('Reverse key malformed')
            accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            sock.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
            return ReverseConnection(sock)
        except BaseException:
            try:sock.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:pass
            sock.close();raise
    def close(self):
        if self.server:self.server.close();self.server=None
