from fractions import Fraction
from .algebra import least_squares
from .checks import number,vector

def reconstruct(track,cameras):
    rows=[]
    for observation in track['observations']:
        p=cameras[observation['camera_id']];xy=vector(observation['xy'],2);weight=number(observation['weight'])
        for index,value in enumerate(xy):
            coefficients=tuple(value*p[2][i]-p[index][i] for i in range(3))
            rhs=p[index][3]-value*p[2][3];rows.append((coefficients,rhs,weight))
    xyz=least_squares(rows)
    if xyz is None:return {'track_id':track['id'],'status':'unobservable','xyz':None,'reprojections':[],'algebraic_squared_error':None}
    augmented=xyz+(Fraction(1),);reprojections=[]
    for observation in track['observations']:
        p=cameras[observation['camera_id']];homogeneous=[sum(a*b for a,b in zip(row,augmented)) for row in p]
        depth=homogeneous[2];xy=vector(observation['xy'],2)
        projected=[homogeneous[i]/depth for i in range(2)] if depth else None
        reprojections.append({'camera_id':observation['camera_id'],'depth':str(depth),'positive_depth':depth>0,
                              'xy':[str(v) for v in projected] if projected is not None else None,
                              'residual_xy':[str(projected[i]-xy[i]) for i in range(2)] if projected is not None else None})
    error=sum((w*(sum(a[i]*xyz[i] for i in range(3))-b)**2 for a,b,w in rows),Fraction(0))
    normal_residual=[sum((w*a[i]*(sum(a[j]*xyz[j] for j in range(3))-b) for a,b,w in rows),Fraction(0)) for i in range(3)]
    if any(normal_residual):raise ValueError('internal least-squares residual failure')
    return {'track_id':track['id'],'status':'reconstructed','xyz':[str(v) for v in xyz],'reprojections':reprojections,
            'algebraic_squared_error':str(error),'normal_equation_residual':[str(v) for v in normal_residual]}
