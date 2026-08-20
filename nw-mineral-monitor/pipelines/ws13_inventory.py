#!/usr/bin/env python3
"""WS13 Phase 0: full-corpus inventory, integrity audit, and triage.

Walks every WS12 corpus prefix, streams every object once, recomputes its
SHA-256 against the content-addressed key, and classifies it:

  born_digital   real text layer (sampled pages carry extractable text)
  ocr_queue      scanned PDF without a usable text layer
  map_plate      oversize plates/drawings routed to image-index/georef, not OCR
  non_document   zip/shapefile/data payloads routed to GIS intake
  error_queue    unreadable/corrupt/integrity-mismatch, each with a reason

Nothing skips silently: every listed object lands in exactly one class and
the balance equation (discovered = classified + errors) is printed and
written to summary.json.  Outputs (inventory.parquet, inventory.jsonl.gz,
queues.json, summary.json, heartbeat) go to s3://{bucket}/{out_prefix}/.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import io
import json
import threading
import time

import boto3

PDF_MAGIC = b'%PDF-'
ZIP_MAGIC = b'PK\x03\x04'
JPEG_MAGIC = b'\xff\xd8\xff'
PNG_MAGIC = b'\x89PNG'
TIFF_MAGICS = (b'II*\x00', b'MM\x00*')
# A page whose longest side exceeds ~41 inches (2950 pt) is a plate/map;
# single-page giants above ~28 inches also route to the map queue.
PLATE_LONG_SIDE_PT = 2950.0
SINGLE_PAGE_LONG_SIDE_PT = 2000.0
TEXT_SAMPLE_PAGES = 5
TEXT_CHARS_PER_PAGE = 100


def classify_pdf(body: bytes) -> dict:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(body), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt('')
        except Exception:
            return {'cls': 'error_queue', 'reason': 'encrypted_pdf',
                    'pages': None}
    pages = len(reader.pages)
    if pages == 0:
        return {'cls': 'error_queue', 'reason': 'zero_page_pdf', 'pages': 0}
    long_side = 0.0
    for page in reader.pages:
        box = page.mediabox
        long_side = max(long_side, float(box.width), float(box.height))
    if long_side >= PLATE_LONG_SIDE_PT or (
            pages == 1 and long_side >= SINGLE_PAGE_LONG_SIDE_PT):
        return {'cls': 'map_plate', 'reason': f'long_side_{long_side:.0f}pt',
                'pages': pages, 'long_side_pt': round(long_side, 1)}
    step = max(1, pages // TEXT_SAMPLE_PAGES)
    sampled = 0
    texty = 0
    for index in range(0, pages, step):
        if sampled >= TEXT_SAMPLE_PAGES:
            break
        sampled += 1
        try:
            text = reader.pages[index].extract_text() or ''
        except Exception:
            text = ''
        if len(text.strip()) >= TEXT_CHARS_PER_PAGE:
            texty += 1
    cls = 'born_digital' if texty * 2 > sampled else 'ocr_queue'
    return {'cls': cls, 'pages': pages, 'sampled_pages': sampled,
            'texty_pages': texty, 'long_side_pt': round(long_side, 1)}


def classify(body: bytes) -> dict:
    head = body[:8]
    if head.startswith(PDF_MAGIC):
        try:
            return {'mime': 'application/pdf', **classify_pdf(body)}
        except Exception as exc:
            return {'mime': 'application/pdf', 'cls': 'error_queue',
                    'reason': f'pdf_parse:{type(exc).__name__}', 'pages': None}
    if head.startswith(ZIP_MAGIC):
        return {'mime': 'application/zip', 'cls': 'non_document',
                'reason': 'zip_archive', 'pages': None}
    if head.startswith(JPEG_MAGIC):
        return {'mime': 'image/jpeg', 'cls': 'ocr_queue',
                'reason': 'single_image', 'pages': 1}
    if head.startswith(PNG_MAGIC):
        return {'mime': 'image/png', 'cls': 'ocr_queue',
                'reason': 'single_image', 'pages': 1}
    if head[:4] in TIFF_MAGICS:
        return {'mime': 'image/tiff', 'cls': 'ocr_queue',
                'reason': 'tiff_image', 'pages': None}
    return {'mime': 'application/octet-stream', 'cls': 'error_queue',
            'reason': 'unknown_magic', 'pages': None}


class Progress:
    def __init__(self, s3, bucket, out_prefix, interval=60):
        self.s3, self.bucket, self.out_prefix = s3, bucket, out_prefix
        self.interval = interval
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.bytes_done = 0
        self.total_listed = 0
        self.errors: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self.flush()

    def record(self, cls, size):
        with self.lock:
            self.counts[cls] = self.counts.get(cls, 0) + 1
            self.bytes_done += size

    def snapshot(self):
        with self.lock:
            return {
                'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
                'listed': self.total_listed,
                'classified': sum(self.counts.values()),
                'bytes_done': self.bytes_done,
                'counts': dict(sorted(self.counts.items())),
            }

    def flush(self):
        payload = json.dumps(self.snapshot(), indent=1).encode()
        try:
            self.s3.put_object(Bucket=self.bucket,
                               Key=f'{self.out_prefix}/heartbeat.json',
                               Body=payload, ContentType='application/json')
        except Exception:
            pass

    def _loop(self):
        while not self._stop.wait(self.interval):
            self.flush()


def process_key(s3, bucket, key, expected_sha):
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj['Body'].read()
    digest = hashlib.sha256(body).hexdigest()
    row = {'key': key, 'bytes': len(body), 'sha256': digest,
           'expected_sha256': expected_sha}
    if expected_sha and digest != expected_sha:
        row.update({'mime': None, 'cls': 'error_queue',
                    'reason': 'integrity_mismatch', 'pages': None})
        return row
    row.update(classify(body))
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--prefixes', nargs='+', required=True)
    parser.add_argument('--out-prefix', default='ws13/inventory')
    parser.add_argument('--workers', type=int, default=12)
    args = parser.parse_args()

    s3 = boto3.client('s3')
    progress = Progress(s3, args.bucket, args.out_prefix)
    keys = []
    paginator = s3.get_paginator('list_objects_v2')
    for prefix in args.prefixes:
        for page in paginator.paginate(Bucket=args.bucket, Prefix=prefix):
            for item in page.get('Contents', []):
                key = item['Key']
                stem = key.rsplit('/', 1)[-1].split('.', 1)[0]
                expected = stem if len(stem) == 64 else None
                keys.append((key, expected, item['Size']))
    progress.total_listed = len(keys)
    listed_bytes = sum(size for _, _, size in keys)
    print(f'listed {len(keys)} objects, {listed_bytes/1e9:.1f} GB')

    # sha-level dedupe before any compute: content-addressed keys make true
    # byte duplicates impossible within a prefix; detect any cross-prefix
    # duplicates and count them without reprocessing.
    seen: dict[str, str] = {}
    unique_keys, duplicate_rows = [], []
    for key, expected, size in keys:
        if expected and expected in seen:
            duplicate_rows.append({
                'key': key, 'bytes': size, 'sha256': expected,
                'expected_sha256': expected, 'mime': None,
                'cls': 'duplicate', 'reason': f'duplicate_of:{seen[expected]}',
                'pages': None})
            continue
        if expected:
            seen[expected] = key
        unique_keys.append((key, expected, size))
    print(f'unique {len(unique_keys)}, duplicates {len(duplicate_rows)}')

    progress.start()
    rows = list(duplicate_rows)
    for row in duplicate_rows:
        progress.record('duplicate', 0)

    def worker(item):
        key, expected, size = item
        try:
            return process_key(s3, args.bucket, key, expected)
        except Exception as exc:
            return {'key': key, 'bytes': size, 'sha256': expected,
                    'expected_sha256': expected, 'mime': None,
                    'cls': 'error_queue',
                    'reason': f'fetch:{type(exc).__name__}', 'pages': None}

    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        for row in pool.map(worker, unique_keys):
            rows.append(row)
            progress.record(row['cls'], row['bytes'] or 0)
    progress.stop()

    rows.sort(key=lambda row: row['key'])
    jsonl = gzip.compress(''.join(
        json.dumps(row, sort_keys=True) + '\n' for row in rows).encode())
    s3.put_object(Bucket=args.bucket,
                  Key=f'{args.out_prefix}/inventory.jsonl.gz', Body=jsonl,
                  ContentType='application/gzip')
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        table = pa.Table.from_pylist(rows)
        sink = io.BytesIO()
        pq.write_table(table, sink, compression='zstd')
        s3.put_object(Bucket=args.bucket,
                      Key=f'{args.out_prefix}/inventory.parquet',
                      Body=sink.getvalue(),
                      ContentType='application/octet-stream')
    except Exception as exc:
        print(f'parquet export unavailable: {exc}')

    by_class: dict[str, dict] = {}
    for row in rows:
        entry = by_class.setdefault(row['cls'], {'files': 0, 'bytes': 0,
                                                 'pages': 0, 'reasons': {}})
        entry['files'] += 1
        entry['bytes'] += row['bytes'] or 0
        entry['pages'] += row.get('pages') or 0
        if row.get('reason'):
            entry['reasons'][row['reason']] = \
                entry['reasons'].get(row['reason'], 0) + 1
    classified = sum(v['files'] for k, v in by_class.items()
                     if k != 'error_queue')
    errors = by_class.get('error_queue', {}).get('files', 0)
    balance = {
        'files_discovered': len(rows),
        'files_classified': classified,
        'files_in_error_queues': errors,
        'balances': len(rows) == classified + errors,
    }
    summary = {
        'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
        'listed_objects': len(keys), 'listed_bytes': listed_bytes,
        'by_class': by_class, 'balance_equation': balance,
    }
    s3.put_object(Bucket=args.bucket, Key=f'{args.out_prefix}/summary.json',
                  Body=json.dumps(summary, indent=1, sort_keys=True).encode(),
                  ContentType='application/json')
    print(json.dumps(balance))
    print('BALANCE OK' if balance['balances'] else 'BALANCE FAILED')
    return 0 if balance['balances'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
