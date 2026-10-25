"""End-to-end tests of /analyze and /convert on a tiny generated Bambu project.

Run with: python -m unittest discover tests   (needs flask)
"""
import io
import json
import os
import re
import sys
import unittest
import zipfile
import xml.etree.ElementTree as ET

REPO = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, REPO)
os.chdir(REPO)  # the app reads its templates and uploads/ relative to the repo

import app as bl  # noqa: E402
import paint      # noqa: E402

PALETTE = ['#101010', '#202020', '#303030', '#404040', '#505050',
           '#606060', '#707070', '#808080', '#909090', '#A0A0A0']


def bambu_project() -> bytes:
    """A 10-filament project saved without slicing: object on filament 6, triangles painted with 1 and 10."""
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        '<resources><object id="1" type="model"><mesh><vertices>'
        '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="10"/>'
        '</vertices><triangles>'
        '<triangle v1="0" v2="1" v3="2" paint_color="7C"/>'   # filament 10
        '<triangle v1="0" v2="1" v3="3" paint_color="041"/>'  # split: filament 1 and the part's own
        '<triangle v1="0" v2="2" v3="3"/>'
        '</triangles></mesh></object></resources>'
        '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 100 100 0"/></build></model>')
    settings = {
        'filament_colour': PALETTE, 'filament_type': ['PLA'] * len(PALETTE),
        'different_settings_to_system': ['', '', ''],
    }
    model_settings = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>'
                      '<object id="1"><metadata key="name" value="test"/><metadata key="extruder" value="6"/>'
                      '<part id="1" subtype="normal_part"><metadata key="extruder" value="0"/></part></object>'
                      '<plate><metadata key="plater_id" value="1"/></plate></config>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('3D/3dmodel.model', model)
        z.writestr('Metadata/project_settings.config', json.dumps(settings))
        z.writestr('Metadata/model_settings.config', model_settings)
        z.writestr('Metadata/slice_info.config', '<?xml version="1.0" encoding="UTF-8"?>\n<config><header/></config>')
    return buf.getvalue()


class Convert(unittest.TestCase):
    def setUp(self):
        self.client = bl.app.test_client()
        r = self.client.post('/analyze', data={'file': (io.BytesIO(bambu_project()), 'test.3mf')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200)
        self.session = r.get_json()['session_id']
        self.filaments = {f['id']: f for f in r.get_json()['filaments']}

    def tearDown(self):
        for name in os.listdir(bl.UPLOAD_FOLDER):
            if name.startswith(self.session):
                os.remove(os.path.join(bl.UPLOAD_FOLDER, name))

    def convert(self, ids):
        return self.client.post('/convert', json={'session_id': self.session, 'colors': {
            i: {'color': PALETTE[int(i) - 1], 'type': 'PLA'} for i in ids}})

    def test_analyze_marks_used_filaments(self):
        self.assertEqual(sorted((i for i, f in self.filaments.items() if f['used']), key=int), ['1', '6', '10'])

    def test_unsliced_project_renumbers_paint_and_extruders(self):
        r = self.convert(['1', '6', '10'])  # become extruders 1, 2, 3
        self.assertEqual(r.status_code, 200, r.get_json())
        out = os.path.join(bl.UPLOAD_FOLDER, f'{self.session}_U1_Ready.3mf')
        with zipfile.ZipFile(out) as z:
            model = z.read('3D/3dmodel.model').decode()
            model_settings = z.read('Metadata/model_settings.config').decode()
            slice_info = ET.fromstring(z.read('Metadata/slice_info.config'))
            cfg = json.loads(z.read('Metadata/project_settings.config'))
        codes = re.findall(r'paint_color="([0-9A-F]+)"', model)
        self.assertEqual([dict(paint.states(c)) for c in codes], [{3: 1}, {1: 1, 0: 1}])
        self.assertIn('key="extruder" value="2"', model_settings)  # the object: filament 6 -> extruder 2
        self.assertEqual(cfg['filament_colour'][:3], ['#101010FF', '#606060FF', '#A0A0A0FF'])
        self.assertEqual([(f.get('id'), f.get('color')) for f in slice_info.iter('filament')][:3],
                         [('1', '#101010'), ('2', '#606060'), ('3', '#A0A0A0')])

    def test_dropping_a_painted_filament_is_refused(self):
        r = self.convert(['1', '6'])
        self.assertEqual(r.status_code, 400)
        self.assertIn('Filament 10', r.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
