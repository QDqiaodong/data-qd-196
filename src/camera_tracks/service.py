from .model import validate
from .reconstruct import reconstruct

def process(data):
    cameras,tracks=validate(data);results=[reconstruct(t,cameras) for t in tracks]
    return {'schema_version':1,'tracks':results,'summary':{'cameras':len(cameras),'tracks':len(results),
            'reconstructed':sum(t['status']=='reconstructed' for t in results),'unobservable':sum(t['status']=='unobservable' for t in results),
            'tracks_with_nonpositive_depth':sum(any(not r['positive_depth'] for r in t['reprojections']) for t in results)}}
