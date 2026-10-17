"""Tokenizer for BM25: codes stay whole, Chinese becomes character bigrams."""
import re
import unicodedata

# Codes such as cl-02, cl02-ov-03, rf-25, hc-230ag, ecn-2026-014, c2-260209d-04, mfg-sop-003
# and plain words or numbers (d09, 0.15) come out as single tokens; CJK runs are split later.
TOKEN = re.compile(r'[a-z0-9]+(?:[-.][a-z0-9]+)*|[㐀-䶿一-鿿]+')
CJK = re.compile(r'[㐀-䶿一-鿿]')


def tokenize(text):
    tokens=[]
    for match in TOKEN.finditer(unicodedata.normalize('NFKC',text).lower()):
        word=match.group()
        if not CJK.match(word):
            tokens.append(word)
        elif len(word)==1:
            tokens.append(word)
        else:
            tokens.extend(word[i:i+2] for i in range(len(word)-1))
    return tokens
