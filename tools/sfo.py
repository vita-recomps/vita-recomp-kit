import struct,sys
# titles in other scripts (Polish, Russian, Japanese ...) must not stop a cp1252 console
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
d=open(sys.argv[1],'rb').read()
m,v,kt,dt,n=struct.unpack_from('<4sIIII',d,0)
for i in range(n):
    ko,fmt,ln,mx,do=struct.unpack_from('<HHIII',d,20+i*16)
    k=d[kt+ko:d.index(b'\0',kt+ko)].decode()
    raw=d[dt+do:dt+do+ln]
    val=struct.unpack('<I',raw)[0] if fmt==0x404 else raw.rstrip(b'\0').decode('utf8','replace')
    print(k,'=',hex(val) if isinstance(val,int) else val)
