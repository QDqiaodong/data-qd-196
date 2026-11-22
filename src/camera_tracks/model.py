from .algebra import rank
from .checks import array,fields,ident,number,unique,vector

def validate(data):
    fields(data,('cameras','tracks'))
    cameras={}
    for camera in array(data['cameras'],2,20):
        fields(camera,('id','projection'));name=ident(camera['id'])
        if name in cameras:raise ValueError('duplicate camera')
        matrix=tuple(vector(row,4) for row in array(camera['projection'],3,3))
        if rank(matrix)!=3:raise ValueError('camera projection must have row rank three')
        cameras[name]=matrix
    tracks=array(data['tracks'],1,200)
    for track in tracks:
        fields(track,('id','observations'));ident(track['id'])
        observations=array(track['observations'],2,20)
        for observation in observations:
            fields(observation,('camera_id','xy','weight'));name=ident(observation['camera_id'])
            if name not in cameras:raise ValueError('unknown camera')
            vector(observation['xy'],2)
            if number(observation['weight'])<=0:raise ValueError('positive observation weight required')
        unique([o['camera_id'] for o in observations])
    unique([t['id'] for t in tracks])
    return cameras,tracks
