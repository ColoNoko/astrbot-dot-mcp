"""Strict display projection of internal media fingerprints; never authorization."""
import json
import re
import unicodedata
PREFIX = '[dot-media-v1]:'
def media_metadata(text):
    if not isinstance(text,str) or not text.startswith(PREFIX) or len(text)>2048:
        return None
    try:
        value=json.loads(text[len(PREFIX):])
        if (not isinstance(value,dict) or set(value)!={'v','kind','filename','sha256','size_bytes'} or
            type(value['v']) is not int or value['v']!=1 or value['kind'] not in ('image','file') or
            type(value['size_bytes']) is not int or not 1<=value['size_bytes']<=20_000_000 or
            not isinstance(value['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',value['sha256']) or
            not isinstance(value['filename'],str) or not 1<=len(value['filename'].encode())<=180 or
            any(c in '/\\:*?"<>|' or unicodedata.category(c).startswith('C') for c in value['filename'])):
            return None
        return value
    except (ValueError,TypeError,UnicodeError):
        return None

def display_reply(row):
    result=dict(row)
    metadata=media_metadata(result.get('text'))
    if metadata is not None:
        result['text']='[附件回复：'+metadata['kind']+' / '+metadata['filename']+'，'+str(metadata['size_bytes'])+'字节；适配器状态见state]'
        result['attachment_reply']=metadata
    return result
