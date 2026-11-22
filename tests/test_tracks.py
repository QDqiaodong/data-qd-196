import json
from pathlib import Path
import unittest
from camera_tracks.service import process

def sample():return json.loads((Path(__file__).resolve().parents[1]/'fixtures/input.json').read_text())

class TrackTests(unittest.TestCase):
    def test_recovery_and_reprojection(self):
        r=process(sample());t=r['tracks'][0]
        self.assertEqual(t['xyz'],['2','1','4'])
        self.assertEqual(t['algebraic_squared_error'],'0')
        self.assertEqual([p['residual_xy'] for p in t['reprojections']],[['0','0'],['0','0']])

    def test_identical_views_unobservable_not_arbitrary_depth(self):
        d=sample();d['cameras'][1]['projection']=d['cameras'][0]['projection'];d['tracks'][0]['observations'][1]['xy']=['0.5','0.25']
        t=process(d)['tracks'][0]
        self.assertEqual(t['status'],'unobservable');self.assertIsNone(t['xyz'])

    def test_inconsistent_vertical_observations_have_evidence(self):
        d=sample();d['tracks'][0]['observations'][1]['xy'][1]='0.3'
        t=process(d)['tracks'][0]
        self.assertNotEqual(t['algebraic_squared_error'],'0')
        self.assertEqual(t['normal_equation_residual'],['0','0','0'])
        self.assertTrue(any(p['residual_xy']!=['0','0'] for p in t['reprojections']))

    def test_behind_views_retained_and_flagged(self):
        d=sample();d['tracks'][0]['observations'][0]['xy']=['-0.5','-0.25'];d['tracks'][0]['observations'][1]['xy']=['-0.25','-0.25']
        r=process(d)
        self.assertEqual(r['tracks'][0]['xyz'],['2','1','-4']);self.assertEqual(r['summary']['tracks_with_nonpositive_depth'],1)

    def test_rank_invalid_reference_and_weight_rejected(self):
        d=sample();d['cameras'][0]['projection'][2]=['0']*4
        with self.assertRaises(ValueError):process(d)
        d=sample();d['tracks'][0]['observations'][1]['camera_id']='left'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['tracks'][0]['observations'][0]['weight']='0'
        with self.assertRaises(ValueError):process(d)

if __name__=='__main__':unittest.main()
