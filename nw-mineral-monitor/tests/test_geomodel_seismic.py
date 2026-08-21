"""SEG-Y sections and LAS well logs."""
import sys, unittest, tempfile, os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines'))

import io
import math
import struct

from geomodel.formats import segy
from geomodel.formats import las


def isnan(v):
    return isinstance(v, float) and v != v


class TempDirMixin(object):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='nwmm-seismic-')

    def path(self, name):
        return os.path.join(self.tmp, name)


def traces(ntr=7, ns=50):
    return [[math.sin(0.3 * i + 0.5 * k) * (k + 1) for i in range(ns)] for k in range(ntr)]


def coords(ntr=7):
    return [(500000 + 10 * k, 4000000 + 5 * k) for k in range(ntr)]


# ---------------------------------------------------------------------- SEG-Y
class TestIBMFloat(unittest.TestCase):
    def test_known_words(self):
        self.assertEqual(segy.ibm_to_float(0x41100000), 1.0)
        self.assertEqual(segy.ibm_to_float(0xC276A000), -118.625)
        self.assertEqual(segy.ibm_to_float(0x40280000), 0.15625)
        self.assertEqual(segy.ibm_to_float(0), 0.0)
        self.assertEqual(segy.float_to_ibm(1.0), 0x41100000)
        self.assertEqual(segy.float_to_ibm(-118.625), 0xC276A000)
        self.assertEqual(segy.float_to_ibm(0.0), 0)

    def test_round_trip_precision(self):
        for v in (1.0, -118.625, 0.15625, 3.14159, 1e-7, 123456.789, -0.001, 7e20, 2.0 ** -70, 65536.0):
            w = segy.float_to_ibm(v)
            back = segy.ibm_to_float(w)
            self.assertLessEqual(abs(back - v), abs(v) * 2 ** -20, v)     # 21..24 significant bits


class TestSEGY(TempDirMixin, unittest.TestCase):
    def test_write_read_round_trip(self):
        tr, xy = traces(), coords()
        p = segy.write_segy(tr, self.path('a.sgy'), 2000, coords=xy)
        self.assertEqual(os.path.getsize(p), 3600 + 7 * (240 + 50 * 4))
        d = segy.read_segy(p)
        self.assertEqual(d['n_traces'], 7)
        self.assertEqual(d['ns'], 50)
        self.assertEqual(d['dt'], 0.002)
        self.assertEqual(d['format'], 5)
        self.assertEqual(d['endian'], 'big')
        self.assertEqual(d['revision'], 1.0)
        self.assertEqual(d['text_encoding'], 'ascii')
        self.assertTrue(d['text_header'].startswith('C 1 nw-mineral-monitor'))
        self.assertEqual(len(d['text_header'].split('\n')), 40)
        bh = d['binary_header']
        self.assertEqual((bh['sample_interval_us'], bh['samples_per_trace'], bh['format_code'], bh['measurement_system'],
                          bh['fixed_length'], bh['n_ext_text'], bh['traces_per_ensemble']), (2000, 50, 5, 1, 1, 0, 1))
        for k in range(7):
            self.assertEqual(len(d['samples'][k]), 50)
            self.assertLess(max(abs(a - b) for a, b in zip(d['samples'][k], tr[k])), 1e-6)
        th = d['trace_headers'][3]
        self.assertEqual((th['seq'], th['ffid'], th['cdp'], th['trace_id'], th['ns'], th['dt_us']), (4, 4, 4, 1, 50, 2000))
        self.assertEqual((th['cdpx'], th['cdpy'], th['sx'], th['sy']), (500030.0, 4000015.0, 500030.0, 4000015.0))
        self.assertEqual((th['inline'], th['xline'], th['sp'], th['scalar_coord']), (1, 4, 4, 1))
        self.assertEqual(d['coords'], [(float(x), float(y)) for x, y in xy])
        self.assertEqual(d['warnings'], [])
        self.assertEqual(d['path'], p)
        b = segy.write_segy(tr, io.BytesIO(), 2000, coords=xy)
        self.assertIsInstance(b, bytes)
        self.assertEqual(segy.read_segy(b)['n_traces'], 7)

    def test_formats_and_endian(self):
        tr = [[round(v * 100) for v in t] for t in traces(3, 20)]
        for fmt in (1, 2, 3, 5, 8):
            vals = [[max(-127, min(127, v)) for v in t] for t in tr] if fmt == 8 else tr
            d = segy.read_segy(segy.write_segy(vals, io.BytesIO(), 1000, format_code=fmt))
            self.assertEqual(d['format'], fmt)
            for k in range(3):
                self.assertLess(max(abs(a - b) for a, b in zip(d['samples'][k], vals[k])), 1e-3, fmt)
        b = segy.write_segy(tr, io.BytesIO(), 1000, endian='little')
        d = segy.read_segy(b)
        self.assertEqual(d['endian'], 'little')
        self.assertTrue(any('little-endian' in w for w in d['warnings']))
        self.assertLess(max(abs(a - b) for a, b in zip(d['samples'][1], tr[1])), 1e-3)
        d = segy.read_segy(b, endian='little')
        self.assertFalse(any('endian' in w for w in d['warnings']))
        # rev 2 byte-order word honoured
        bb = bytearray(b)
        struct.pack_into('<I', bb, 3200 + 96, 16909060)
        d = segy.read_segy(bytes(bb))
        self.assertEqual(d['endian'], 'little')
        self.assertFalse(any('endian' in w for w in d['warnings']))
        self.assertTrue(any('no trace coordinates' in w for w in d['warnings']))

    def test_text_encodings_and_scalars(self):
        tr = traces(2, 10)
        b = segy.write_segy(tr, io.BytesIO(), 500, text='HELLO\nWORLD', text_encoding='ebcdic')
        self.assertEqual(b[0], 0xC3)                                           # EBCDIC 'C'
        d = segy.read_segy(b)
        self.assertEqual(d['text_encoding'], 'ebcdic')
        self.assertTrue(d['text_header'].startswith('C 1 HELLO\nC 2 WORLD\nC 3'))
        b = segy.write_segy(tr, io.BytesIO(), 500, text='C 1 CUSTOM')
        self.assertEqual(segy.read_segy(b)['text_header'].split('\n')[0], 'C 1 CUSTOM')
        # coordinate / elevation scalars: negative = divide, positive = multiply
        bb = bytearray(segy.write_segy(tr, io.BytesIO(), 500, coords=[(123456, 654321), (1, 2)]))
        h0 = 3600
        struct.pack_into('>h', bb, h0 + 70, -100)
        struct.pack_into('>h', bb, h0 + 68, 10)
        struct.pack_into('>i', bb, h0 + 40, 55)
        d = segy.read_segy(bytes(bb))
        th = d['trace_headers'][0]
        self.assertEqual((th['cdpx'], th['cdpy'], th['sx']), (1234.56, 6543.21, 1234.56))
        self.assertEqual(th['rec_elev'], 550)
        self.assertEqual(d['coords'][0], (1234.56, 6543.21))
        # no CDP coordinates -> source coordinates
        struct.pack_into('>i', bb, h0 + 180, 0)
        struct.pack_into('>i', bb, h0 + 184, 0)
        self.assertEqual(segy.read_segy(bytes(bb))['coords'][0], (1234.56, 6543.21))

    def test_variable_trace_length_and_extended_text(self):
        tr = traces(2, 10)
        base = bytearray(segy.write_segy(tr, io.BytesIO(), 1000))
        struct.pack_into('>h', base, 3200 + 302, 0)                            # fixed-length flag off
        short = bytearray(240)
        struct.pack_into('>H', short, 114, 4)
        struct.pack_into('>H', short, 116, 1000)
        blob = bytes(base) + bytes(short) + struct.pack('>4f', 1, 2, 3, 4)
        d = segy.read_segy(blob)
        self.assertEqual(d['n_traces'], 3)
        self.assertEqual([len(s) for s in d['samples']], [10, 10, 4])
        self.assertEqual(list(d['samples'][2]), [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(d['ns'], 10)
        self.assertTrue(any('variable trace lengths' in w for w in d['warnings']))
        # one extended textual header inserted after the binary header
        ext = bytearray(segy.write_segy(tr, io.BytesIO(), 1000))
        struct.pack_into('>h', ext, 3200 + 304, 1)
        blob = bytes(ext[:3600]) + b'C 1 EXTENDED'.ljust(3200) + bytes(ext[3600:])
        d = segy.read_segy(blob)
        self.assertEqual(d['n_traces'], 2)
        self.assertEqual(d['binary_header']['n_ext_text'], 1)
        self.assertTrue(any('extended textual header' in w for w in d['warnings']))
        self.assertLess(max(abs(a - b) for a, b in zip(d['samples'][1], tr[1])), 1e-6)
        with self.assertRaises(ValueError):
            segy.read_segy(b'short')
        bad = bytearray(segy.write_segy(tr, io.BytesIO(), 1000))
        struct.pack_into('>h', bad, 3200 + 24, 4)
        with self.assertRaises(ValueError):
            segy.read_segy(bytes(bad))

    def test_section_image(self):
        tr = traces(5, 30)
        d = segy.read_segy(segy.write_segy(tr, io.BytesIO(), 4000, coords=coords(5)))
        img = segy.section_image(d)
        self.assertEqual((img['width'], img['height']), (5, 30))
        self.assertEqual(len(img['gray']), 150)
        self.assertEqual(img['p1'], [500000.0, 4000000.0])
        self.assertEqual(img['p2'], [500040.0, 4000020.0])
        self.assertEqual(img['z_top'], 0.0)
        self.assertAlmostEqual(img['z_bottom'], -(30 - 1) * 0.004 * 1000)
        self.assertTrue(any('pseudo-depth' in w for w in img['warnings']))
        # row 0 = first sample of every trace; trace 0 sample 0 is sin(0) = 0 -> mid grey
        self.assertEqual(img['gray'][0], 128)
        # the loudest trace (k = 4) reaches the clip level -> near 255 / 0 somewhere in its column
        col = [img['gray'][r * 5 + 4] for r in range(30)]
        self.assertGreaterEqual(max(col), 250)
        self.assertLessEqual(min(col), 5)
        img2 = segy.section_image(d, z_top=1200.0, z_bottom=900.0, clip_pct=100)
        self.assertEqual((img2['z_top'], img2['z_bottom']), (1200.0, 900.0))
        self.assertEqual(img2['warnings'], [])

    def test_segyio_reads_ours(self):
        try:
            import segyio
            import numpy as np
        except ImportError:
            self.skipTest('segyio not installed')
        tr, xy = traces(), coords()
        for fmt in (5, 1):
            p = segy.write_segy(tr, self.path('ours_%d.sgy' % fmt), 2000, coords=xy, format_code=fmt,
                                text='TEST HEADER', text_encoding='ebcdic')
            with segyio.open(p, ignore_geometry=True) as f:
                self.assertEqual(f.tracecount, 7)
                self.assertEqual(f.samples.size, 50)
                self.assertEqual(segyio.tools.dt(f), 2000.0)
                self.assertEqual(int(f.format), fmt)
                for k in range(7):
                    rel = np.abs(np.asarray(f.trace[k]) - np.asarray(tr[k])).max() / np.abs(tr[k]).max()
                    self.assertLess(rel, 1e-5)
                h = f.header[3]
                self.assertEqual(h[segyio.TraceField.CDP_X], 500030)
                self.assertEqual(h[segyio.TraceField.CDP_Y], 4000015)
                self.assertEqual(h[segyio.TraceField.SourceX], 500030)
                self.assertEqual(h[segyio.TraceField.SourceGroupScalar], 1)
                self.assertEqual(h[segyio.TraceField.INLINE_3D], 1)
                self.assertEqual(h[segyio.TraceField.CROSSLINE_3D], 4)
                self.assertEqual(h[segyio.TraceField.TRACE_SEQUENCE_LINE], 4)
                self.assertEqual(h[segyio.TraceField.TRACE_SAMPLE_COUNT], 50)
                self.assertEqual(h[segyio.TraceField.TRACE_SAMPLE_INTERVAL], 2000)
                self.assertEqual(f.bin[segyio.BinField.Format], fmt)
                self.assertIn(f.bin[segyio.BinField.SEGYRevision], (1, 256))    # major byte 1 / 0x0100
                self.assertTrue(f.text[0].startswith(b'C 1 TEST HEADER'))

    def test_ours_reads_segyio(self):
        try:
            import segyio
            import numpy as np
        except ImportError:
            self.skipTest('segyio not installed')
        data = np.array(traces(6, 40), dtype=np.float32)
        # from_array2D: IBM float (segyio default) and IEEE
        for fmt in (segyio.SegySampleFormat.IBM_FLOAT_4_BYTE, segyio.SegySampleFormat.IEEE_FLOAT_4_BYTE):
            p = self.path('sio_%d.sgy' % int(fmt))
            segyio.tools.from_array2D(p, data, dt=2000, format=fmt)
            d = segy.read_segy(p)
            self.assertEqual(d['format'], int(fmt))
            self.assertEqual(d['n_traces'], 6)
            self.assertEqual(d['ns'], 40)
            self.assertEqual(d['dt'], 0.002)
            self.assertEqual(d['text_encoding'], 'ebcdic')
            for k in range(6):
                rel = np.abs(np.asarray(d['samples'][k]) - data[k]).max() / np.abs(data[k]).max()
                self.assertLess(rel, 1e-5)
            self.assertEqual(d['trace_headers'][2]['ns'], 40)
        # spec-created file: int16 samples, scaled coordinates, inline / crossline
        ns, ntr = 40, 6
        spec = segyio.spec()
        spec.format = 3
        spec.samples = range(ns)
        spec.tracecount = ntr
        spec.sorting = 2
        p = self.path('sio_spec.sgy')
        with segyio.create(p, spec) as f:
            f.text[0] = segyio.tools.create_text_header({1: 'C 1 SPEC FILE', 2: 'C 2 LINE TWO'})
            f.bin.update(hdt=2000, hns=ns, format=3, rev=1)
            for k in range(ntr):
                f.header[k] = {segyio.TraceField.TRACE_SAMPLE_COUNT: ns, segyio.TraceField.TRACE_SAMPLE_INTERVAL: 2000,
                               segyio.TraceField.SourceGroupScalar: -100,
                               segyio.TraceField.CDP_X: int(round((500000 + 0.25 * k) * 100)),
                               segyio.TraceField.CDP_Y: int(4000000 * 100),
                               segyio.TraceField.INLINE_3D: 10, segyio.TraceField.CROSSLINE_3D: k + 1,
                               segyio.TraceField.DelayRecordingTime: 5}
                f.trace[k] = np.round(data[k] * 1000).astype(np.int16).astype(np.float32)
        d = segy.read_segy(p)
        self.assertEqual(d['format'], 3)
        self.assertEqual(d['text_encoding'], 'ebcdic')
        self.assertTrue(d['text_header'].startswith('C 1 C 1 SPEC FILE'))
        self.assertEqual(d['coords'][1], (500000.25, 4000000.0))
        th = d['trace_headers'][2]
        self.assertEqual((th['inline'], th['xline'], th['delay_ms'], th['scalar_coord']), (10, 3, 5, -100))
        for k in range(ntr):
            self.assertEqual(list(d['samples'][k]), [float(v) for v in np.round(data[k] * 1000).astype(np.int16)])
        img = segy.section_image(d)
        self.assertEqual((img['width'], img['height']), (ntr, ns))


# ------------------------------------------------------------------------ LAS
WRAPPED = """~VERSION INFORMATION
 VERS.                 1.2:   CWLS LOG ASCII STANDARD -VERSION 1.2
 WRAP.                 YES:   MULTIPLE LINES PER DEPTH STEP
~WELL INFORMATION
 STRT.M                 910.000:
 STOP.M                 911.000:
 STEP.M                 0.500:
 NULL.                 -999.2500:
 WELL.                 W1     : WELL
 DATE.                 13-DEC-86 12:30:00 : LOG DATE
~CURVE INFORMATION
 DEPT.M                   :  DEPTH
 DT  .US/M               :  SONIC
 RHOB.K/M3               :  DENSITY
 NPHI.V/V                :  NEUTRON
 SFLU.OHMM               :  RESISTIVITY
# a comment line
~A
 910.000
   -999.250   2550.000      0.450
      123.450
 910.500
    176.500   2450.000      0.350
      -999.250
 911.000
    177.000   2460.000      0.300
      130.000
"""

LAS3 = """~Version
 VERS. 3.0 : CWLS LOG ASCII STANDARD - VERSION 3.0
 WRAP. NO : One line per depth step
 DLM . COMMA : Column Data Section Delimiter
~Well
 STRT .M 1000.0 : Start
 STOP .M 1001.0 : Stop
 STEP .M 0.5 : Step
 NULL . -999.25 : Null
 WELL . L3 : Well
~Log_Parameter
 RUN . 1 : Run
~Log_Definition
 DEPT .M : Depth {F}
 GR .API : Gamma {F}
 RES .OHMM : Resistivity {F}
~Log_Data | Log_Definition
1000.0,50.5,10
1000.5,,-999.25
1001.0,55.0,30
~Core_Definition
 CORE .M : core
~Core_Data | Core_Definition
1,2
"""


class TestLAS(TempDirMixin, unittest.TestCase):
    def test_header_line_parsing(self):
        self.assertEqual(las.parse_header_line(' STRT.M   1670.0 : START DEPTH'), ('STRT', 'M', '1670.0', 'START DEPTH'))
        self.assertEqual(las.parse_header_line('DATE.  13-DEC-86 12:30:00 : LOG DATE'), ('DATE', '', '13-DEC-86 12:30:00', 'LOG DATE'))
        self.assertEqual(las.parse_header_line(' DT  .US/M               :  SONIC'), ('DT', 'US/M', '', 'SONIC'))
        self.assertEqual(las.parse_header_line('NULL.  -999.25:'), ('NULL', '', '-999.25', ''))
        self.assertEqual(las.parse_header_line('WELL: ABC'), ('WELL', '', 'ABC', ''))

    def test_wrap_yes(self):
        d = las.read_las(WRAPPED.encode())
        self.assertEqual(d['version'], 1.2)
        self.assertTrue(d['wrap'])
        self.assertEqual([c['mnem'] for c in d['curves']], ['DEPT', 'DT', 'RHOB', 'NPHI', 'SFLU'])
        self.assertEqual(d['curves'][1]['unit'], 'US/M')
        self.assertEqual(d['index_unit'], 'M')
        self.assertEqual(d['n_rows'], 3)
        self.assertEqual(list(d['data']['DEPT']), [910.0, 910.5, 911.0])
        self.assertTrue(isnan(d['data']['DT'][0]))
        self.assertEqual(list(d['data']['DT'][1:]), [176.5, 177.0])
        self.assertEqual(list(d['data']['RHOB']), [2550.0, 2450.0, 2460.0])
        self.assertTrue(isnan(d['data']['SFLU'][1]))
        self.assertEqual(d['well']['WELL']['value'], 'W1')
        self.assertEqual(d['well']['DATE']['value'], '13-DEC-86 12:30:00')
        self.assertEqual(d['null'], -999.25)
        self.assertEqual(d['warnings'], [])
        iv = las.las_to_intervals(d, 'W1')
        self.assertEqual(len(iv), 3)
        self.assertEqual(iv[0]['hole'], 'W1')
        self.assertAlmostEqual(iv[0]['from'], 909.75)
        self.assertAlmostEqual(iv[0]['to'], 910.25)
        self.assertIsNone(iv[0]['DT'])
        self.assertEqual(iv[1]['RHOB'], 2450.0)
        self.assertEqual(las.las_to_intervals(d, 'W1', step=2.0)[2]['to'], 912.0)
        self.assertEqual(set(las.las_to_intervals(d, 'W1', curves=['DT'])[0]), {'hole', 'from', 'to', 'DT'})

    def test_wrap_no_duplicates_and_round_trip(self):
        text = ("~V\nVERS. 2.0:\nWRAP. NO:\n~W\nSTRT.M 100:\nSTOP.M 101:\nSTEP.M 0.5:\nNULL. -9999:\nWELL. X-1: well\n"
                "~C\nDEPT.M:\nGR.API:\nGR.API: second gamma\n~P\nMUD. KCL: Mud\n~O\nfree text\nmore\n"
                "~A\n100 1 2\n100.5 -9999 3\n101 5\n")
        d = las.read_las(text.encode())
        self.assertEqual([c['mnem'] for c in d['curves']], ['DEPT', 'GR:1', 'GR:2'])
        self.assertEqual(list(d['data']['GR:2'][:2]), [2.0, 3.0])
        self.assertTrue(isnan(d['data']['GR:1'][1]))
        self.assertTrue(isnan(d['data']['GR:2'][2]))                          # padded short line
        self.assertEqual(d['params']['MUD']['value'], 'KCL')
        self.assertEqual(d['other'], 'free text\nmore')
        self.assertTrue(any('duplicate' in w for w in d['warnings']))
        self.assertTrue(any('fewer values' in w for w in d['warnings']))
        p = las.write_las(d, self.path('out.las'))
        out = Path(p).read_text()
        self.assertIn('VERS.                 2.0', out)
        self.assertIn('WRAP.                  NO', out)
        self.assertIn('~ASCII Log Data', out)
        self.assertIn('-9999', out)
        d2 = las.read_las(p)
        self.assertEqual([c['mnem'] for c in d2['curves']], ['DEPT', 'GR:1', 'GR:2'])
        self.assertEqual(list(d2['data']['DEPT']), [100.0, 100.5, 101.0])
        self.assertTrue(isnan(d2['data']['GR:1'][1]))
        self.assertEqual(d2['well']['WELL']['value'], 'X-1')
        self.assertEqual(d2['well']['STEP']['value'], '0.5')
        self.assertEqual(d2['params']['MUD']['value'], 'KCL')
        self.assertEqual(d2['other'], 'free text\nmore')
        b = las.write_las(d, io.BytesIO())
        self.assertIsInstance(b, bytes)

    def test_las3_best_effort(self):
        d = las.read_las(LAS3.encode())
        self.assertEqual(d['version'], 3.0)
        self.assertEqual(d['delimiter'], ',')
        self.assertEqual([c['mnem'] for c in d['curves']], ['DEPT', 'GR', 'RES'])
        self.assertEqual(list(d['data']['DEPT']), [1000.0, 1000.5, 1001.0])
        self.assertTrue(isnan(d['data']['GR'][1]))                             # empty field
        self.assertTrue(isnan(d['data']['RES'][1]))                            # NULL
        self.assertEqual(d['params']['RUN']['value'], '1')
        self.assertTrue(any('LAS 3.0' in w for w in d['warnings']))
        self.assertTrue(any('Core_Data' in w for w in d['warnings']))

    def test_lasio_cross_check(self):
        try:
            import lasio
            import numpy as np
        except ImportError:
            self.skipTest('lasio not installed')
        # lasio writes -> ours
        l = lasio.LASFile()
        l.well.WELL = 'TEST-1'
        l.well.COMP = 'ACME'
        l.well.DATE = '2024-01-02 12:30:00'
        depth = np.arange(100.0, 110.0, 0.5)
        gr = np.sin(depth)
        gr[3] = np.nan
        l.append_curve('DEPT', depth, unit='M', descr='Depth')
        l.append_curve('GR', gr, unit='API', descr='Gamma')
        l.append_curve('RES', depth * 2, unit='OHMM')
        l.params['MUD'] = lasio.HeaderItem('MUD', '', 'KCL', 'Mud type')
        l.other = 'Some other text\nline 2'
        p = self.path('lasio.las')
        l.write(p, version=2)
        d = las.read_las(p)
        self.assertEqual(d['version'], 2.0)
        self.assertFalse(d['wrap'])
        self.assertEqual([(c['mnem'], c['unit']) for c in d['curves']], [('DEPT', 'M'), ('GR', 'API'), ('RES', 'OHMM')])
        self.assertEqual(d['n_rows'], 20)
        self.assertEqual(d['well']['WELL']['value'], 'TEST-1')
        self.assertEqual(d['well']['DATE']['value'], '2024-01-02 12:30:00')
        self.assertEqual(d['params']['MUD']['value'], 'KCL')
        self.assertEqual(d['other'], 'Some other text\nline 2')
        self.assertTrue(isnan(d['data']['GR'][3]))
        self.assertTrue(np.allclose(np.nan_to_num(np.asarray(d['data']['GR'])), np.nan_to_num(gr), atol=1e-4))
        self.assertEqual(list(d['data']['RES'][:3]), [200.0, 201.0, 202.0])
        self.assertEqual(d['warnings'], [])
        # ours -> lasio
        p2 = las.write_las(d, self.path('ours.las'))
        l2 = lasio.read(p2)
        self.assertEqual(l2.version.VERS.value, 2.0)
        self.assertEqual(l2.well.WELL.value, 'TEST-1')
        self.assertEqual(l2.well.DATE.value, '2024-01-02 12:30:00')
        self.assertEqual(l2.curves.keys(), ['DEPT', 'GR', 'RES'])
        self.assertTrue(np.isnan(l2['GR'][3]))
        self.assertTrue(np.allclose(np.nan_to_num(l2['GR']), np.nan_to_num(gr), atol=1e-4))
        self.assertEqual(list(l2['RES'][:3]), [200.0, 201.0, 202.0])
        self.assertEqual(l2.params['MUD'].value, 'KCL')
        self.assertEqual(l2.other.strip(), 'Some other text\nline 2')
        # WRAP YES fixture: lasio and ours agree
        p3 = self.path('wrap.las')
        Path(p3).write_text(WRAPPED)
        lw = lasio.read(p3)
        d3 = las.read_las(p3)
        for c in ('DEPT', 'DT', 'RHOB', 'NPHI', 'SFLU'):
            self.assertTrue(np.allclose(np.nan_to_num(lw[c]), np.nan_to_num(np.asarray(d3['data'][c]))), c)
            self.assertTrue((np.isnan(lw[c]) == np.isnan(np.asarray(d3['data'][c]))).all(), c)


if __name__ == '__main__':
    unittest.main()
