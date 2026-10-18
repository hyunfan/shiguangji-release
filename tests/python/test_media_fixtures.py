"""Complete tiny PNG and synthetic container fixtures (no external codecs)."""
import struct
import zlib


def png(padding=0):
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))
            + (chunk(b'tEXt', b'Comment\0'+b'x'*padding) if padding else b'')
            + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0'))+chunk(b'IEND',b''))


def box(kind, data=b''):
    return (8+len(data)).to_bytes(4,'big')+kind+data


PNG = png()
VIDEO = box(b'ftyp',b'isom\0\0\0\0')+box(b'moov',box(b'trak',box(b'tkhd',b'track')))+box(b'mdat',b'video data')
FRAGMENTED = VIDEO[: -18]+box(b'moof',box(b'traf',b'fragment'))+box(b'mdat',b'video data')


def ebml(tag, data):
    assert len(data)<127
    return tag.to_bytes((tag.bit_length()+7)//8,'big')+bytes([128+len(data)])+data


WEBM = ebml(0x1A45DFA3,ebml(0x4282,b'webm'))+b'\x18\x53\x80\x67\xff'+ebml(0x1654AE6B,ebml(0xAE,b'\x86\x85V_VP8'))+ebml(0x1F43B675,ebml(0xA3,b'\x81\0\0\x80frame'))
