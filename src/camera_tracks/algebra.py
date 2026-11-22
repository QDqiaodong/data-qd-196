from fractions import Fraction

def rank(rows):
    a=[list(row) for row in rows];pivot=0
    for col in range(len(a[0])):
        chosen=next((i for i in range(pivot,len(a)) if a[i][col]),None)
        if chosen is None:continue
        a[pivot],a[chosen]=a[chosen],a[pivot]
        divisor=a[pivot][col];a[pivot]=[v/divisor for v in a[pivot]]
        for i in range(pivot+1,len(a)):
            multiple=a[i][col];a[i]=[v-multiple*w for v,w in zip(a[i],a[pivot])]
        pivot+=1
        if pivot==len(a):break
    return pivot

def least_squares(rows):
    normal=[[sum((w*a[i]*a[j] for a,b,w in rows),Fraction(0)) for j in range(3)] for i in range(3)]
    rhs=[sum((w*a[i]*b for a,b,w in rows),Fraction(0)) for i in range(3)]
    if rank(normal)<3:return None
    a=[row+[value] for row,value in zip(normal,rhs)]
    for col in range(3):
        chosen=next(i for i in range(col,3) if a[i][col]);a[col],a[chosen]=a[chosen],a[col]
        divisor=a[col][col];a[col]=[x/divisor for x in a[col]]
        for i in range(3):
            if i!=col:
                factor=a[i][col];a[i]=[x-factor*y for x,y in zip(a[i],a[col])]
    return tuple(a[i][3] for i in range(3))
