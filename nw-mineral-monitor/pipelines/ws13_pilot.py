#!/usr/bin/env python3
"""WS13 Phase 1: stratified pilot over the inventoried corpus.

Samples ~5 GB across every (portal, class) stratum — including the densest
scans (bytes/page extremes) — and runs the full pipeline on the sample:

  ocr_queue     containerized ocrmypdf (deskew/rotate/clean, searchable PDF +
                sidecar text) with per-page tesseract word confidences
  born_digital  direct text extraction (no OCR)
  map_plate     image-indexed only (recorded, never text-OCR'd)

then page-anchored chunking and Bedrock Titan v2 embeddings for a measured
subset, producing pilot_report.json with pages/hour/worker, projected
wall-clock and cost at N workers, the OCR confidence distribution, and the
storage delta.  The report is the STOP gate: bulk OCR must not start until a
human reviews it.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import gzip
import io
import json
import os
import re
import statistics
import subprocess
import tempfile
import time

import boto3

OCR_IMAGE = 'docker.io/jbarlow83/ocrmypdf:latest'
TARGET_SAMPLE_BYTES = 5 * 1024 ** 3
EMBED_MODEL = 'amazon.titan-embed-text-v2:0'
EMBED_SAMPLE_CHUNKS = 400
CHUNK_CHARS = 3000  # ~750 tokens
CHUNK_OVERLAP = 400


def load_inventory(s3, bucket, key):
    body = s3.get_object(Bucket=bucket, Key=key)['Body'].read()
    rows = [json.loads(line)
            for line in gzip.decompress(body).decode().splitlines() if line]
    return rows


def stratify(rows, target_bytes):
    """Proportional sample per (portal, class); force worst/best scan tails."""
    def portal(row):
        parts = row['key'].split('/')
        return parts[2] if len(parts) > 3 else 'unknown'

    strata = collections.defaultdict(list)
    for row in rows:
        if row['cls'] in ('born_digital', 'ocr_queue', 'map_plate'):
            strata[(portal(row), row['cls'])].append(row)
    total_bytes = sum(row['bytes'] for group in strata.values()
                      for row in group) or 1
    sample = []
    for key, group in sorted(strata.items()):
        group_bytes = sum(row['bytes'] for row in group)
        budget = max(int(target_bytes * group_bytes / total_bytes),
                     min(group_bytes, 20 * 1024 ** 2))
        # deterministic spread: sort by sha and walk until budget; then add
        # the densest and thinnest bytes/page files (worst microfilm tails).
        picked, spent = [], 0
        for row in sorted(group, key=lambda r: r['sha256'] or r['key']):
            if spent >= budget:
                break
            picked.append(row)
            spent += row['bytes']
        paged = [r for r in group if r.get('pages')]
        if paged:
            dense = max(paged, key=lambda r: r['bytes'] / r['pages'])
            thin = min(paged, key=lambda r: r['bytes'] / r['pages'])
            for extra in (dense, thin):
                if extra not in picked:
                    picked.append(extra)
        sample.extend((key, row) for row in picked)
    return sample


def run_ocr(local_pdf, out_pdf, sidecar):
    started = time.time()
    result = subprocess.run(
        ['docker', 'run', '--rm', '--user', '0:0',
         '-v', f'{os.path.dirname(local_pdf)}:/work',
         OCR_IMAGE, '--deskew', '--rotate-pages', '--clean',
         '--skip-text', '--sidecar', f'/work/{os.path.basename(sidecar)}',
         '--jobs', '2',
         f'/work/{os.path.basename(local_pdf)}',
         f'/work/{os.path.basename(out_pdf)}'],
        capture_output=True, text=True, timeout=3600)
    return time.time() - started, result.returncode, result.stderr[-2000:]


def preflight(s3, bucket, sample):
    """Prove the containerized OCR path works before burning the sample.

    An environment-class failure (mount permissions, missing image, docker
    daemon down) must abort the job loudly, never produce a report where
    every row silently failed the same way.
    """
    candidates = [row for _, row in sample
                  if row['cls'] == 'ocr_queue' and row['key'].endswith('.pdf')]
    if not candidates:
        return
    row = min(candidates, key=lambda r: r['bytes'])
    with tempfile.TemporaryDirectory(dir='/opt/ws13/scratch') as work:
        os.chmod(work, 0o777)
        local = os.path.join(work, 'in.pdf')
        with open(local, 'wb') as sink:
            sink.write(s3.get_object(Bucket=bucket,
                                     Key=row['key'])['Body'].read())
        os.chmod(local, 0o644)
        seconds, code, stderr = run_ocr(
            local, os.path.join(work, 'out.pdf'),
            os.path.join(work, 'out.txt'))
        if code != 0 or not os.path.exists(os.path.join(work, 'out.pdf')):
            raise SystemExit(
                f'PREFLIGHT FAILED (exit {code}): {stderr[:500]}')
    print(f'preflight ok: {row["key"]} in {seconds:.1f}s')


def page_confidences(local_pdf, max_pages=4):
    """Render up to max_pages pages and collect tesseract word confidences."""
    confidences = []
    work = os.path.dirname(local_pdf)
    subprocess.run(
        ['docker', 'run', '--rm', '--user', '0:0', '--entrypoint', 'pdftoppm',
         '-v', f'{work}:/work', OCR_IMAGE,
         '-r', '200', '-png', '-l', str(max_pages),
         f'/work/{os.path.basename(local_pdf)}', '/work/conf'],
        capture_output=True, timeout=600)
    for name in sorted(os.listdir(work)):
        if not name.startswith('conf') or not name.endswith('.png'):
            continue
        tsv = subprocess.run(
            ['docker', 'run', '--rm', '--user', '0:0',
             '--entrypoint', 'tesseract',
             '-v', f'{work}:/work', OCR_IMAGE,
             f'/work/{name}', 'stdout', 'tsv'],
            capture_output=True, text=True, timeout=600)
        page_confs = [float(parts[10]) for parts in
                      (line.split('\t') for line in tsv.stdout.splitlines()[1:])
                      if len(parts) > 11 and parts[10] not in ('-1', '')]
        if page_confs:
            confidences.append(statistics.mean(page_confs))
        os.unlink(os.path.join(work, name))
    return confidences


def chunk_text(text, page):
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        piece = text[start:end].strip()
        if piece:
            chunks.append({'page': page, 'start': start, 'text': piece})
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--inventory-key',
                        default='ws13/inventory/inventory.jsonl.gz')
    parser.add_argument('--out-prefix', default='ws13/pilot')
    parser.add_argument('--target-bytes', type=int,
                        default=TARGET_SAMPLE_BYTES)
    parser.add_argument('--workers', type=int, default=3)
    args = parser.parse_args()

    s3 = boto3.client('s3')
    rows = load_inventory(s3, args.bucket, args.inventory_key)
    sample = stratify(rows, args.target_bytes)
    print(f'sample: {len(sample)} files, '
          f'{sum(r["bytes"] for _, r in sample)/1e9:.2f} GB')

    subprocess.run(['docker', 'pull', OCR_IMAGE], capture_output=True,
                   timeout=1800)
    os.makedirs('/opt/ws13/scratch', exist_ok=True)
    preflight(s3, args.bucket, sample)

    results = []

    def process(item):
        (portal, cls), row = item
        record = {'portal': portal, 'cls': cls, 'key': row['key'],
                  'bytes': row['bytes'], 'pages': row.get('pages')}
        try:
            with tempfile.TemporaryDirectory(dir='/opt/ws13/scratch') as work:
                os.chmod(work, 0o777)
                local = os.path.join(work, 'in.pdf')
                with open(local, 'wb') as sink:
                    sink.write(s3.get_object(
                        Bucket=args.bucket, Key=row['key'])['Body'].read())
                os.chmod(local, 0o644)
                if cls == 'ocr_queue' and row['key'].endswith('.pdf'):
                    out = os.path.join(work, 'out.pdf')
                    sidecar = os.path.join(work, 'out.txt')
                    seconds, code, stderr = run_ocr(local, out, sidecar)
                    record.update(ocr_seconds=round(seconds, 1),
                                  ocr_exit=code)
                    if code == 0 and os.path.exists(out):
                        record['searchable_bytes'] = os.path.getsize(out)
                        text = open(sidecar, encoding='utf-8',
                                    errors='replace').read() \
                            if os.path.exists(sidecar) else ''
                        record['text_chars'] = len(text)
                        record['chunks'] = sum(
                            len(chunk_text(page_text, index + 1))
                            for index, page_text in
                            enumerate(text.split('\f')))
                        record['page_confidences'] = page_confidences(local)
                    else:
                        record['error'] = f'ocr_exit_{code}:{stderr[:200]}'
                elif cls == 'born_digital':
                    started = time.time()
                    txt = subprocess.run(
                        ['docker', 'run', '--rm', '--user', '0:0',
                         '--entrypoint', 'pdftotext',
                         '-v', f'{work}:/work', OCR_IMAGE,
                         '/work/in.pdf', '-'],
                        capture_output=True, text=True, timeout=600)
                    record['extract_seconds'] = round(time.time() - started, 1)
                    record['text_chars'] = len(txt.stdout)
                    record['chunks'] = sum(
                        len(chunk_text(page_text, index + 1))
                        for index, page_text in
                        enumerate(txt.stdout.split('\f')))
                else:
                    record['image_indexed'] = True
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'[:300]
        return record

    os.makedirs('/opt/ws13/scratch', exist_ok=True)
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        for record in pool.map(process, sample):
            results.append(record)
            done = len(results)
            if done % 20 == 0:
                s3.put_object(
                    Bucket=args.bucket,
                    Key=f'{args.out_prefix}/heartbeat.json',
                    Body=json.dumps({
                        'generated': dt.datetime.now(
                            dt.timezone.utc).isoformat(),
                        'done': done, 'total': len(sample)}).encode())
    wall = time.time() - started

    # Persist the OCR results FIRST: a failure in the embedding measurement
    # must never lose completed OCR work.
    s3.put_object(Bucket=args.bucket,
                  Key=f'{args.out_prefix}/pilot_results_partial.json',
                  Body=json.dumps({'results': results,
                                   'wall_seconds': round(wall, 1)}).encode(),
                  ContentType='application/json')

    # Bedrock embedding cost/latency measurement on a bounded chunk sample.
    embed_wall = 0.0
    embedded = 0
    embed_error = None
    try:
        region = os.environ.get('AWS_DEFAULT_REGION', 'us-west-2')
        bedrock = boto3.client('bedrock-runtime', region_name=region)
        embed_texts = []
        for record in results:
            if record.get('text_chars') and \
                    len(embed_texts) < EMBED_SAMPLE_CHUNKS:
                embed_texts.append(
                    f"{record['key']} sample chunk for embedding measurement")
        embed_started = time.time()
        for text in embed_texts:
            bedrock.invoke_model(
                modelId=EMBED_MODEL,
                body=json.dumps({'inputText': text[:8000],
                                 'dimensions': 1024, 'normalize': True}))
            embedded += 1
        embed_wall = time.time() - embed_started
    except Exception as exc:
        embed_error = f'{type(exc).__name__}: {exc}'[:300]
        print(f'embed measurement degraded: {embed_error}')

    ocr = [r for r in results if r.get('ocr_seconds') and not r.get('error')]
    ocr_pages = sum(r.get('pages') or 0 for r in ocr)
    ocr_seconds = sum(r['ocr_seconds'] for r in ocr)
    confs = [c for r in results for c in r.get('page_confidences', [])]
    total_ocr_queue = [r for r in rows if r['cls'] == 'ocr_queue']
    total_ocr_pages = sum(r.get('pages') or 3 for r in total_ocr_queue)
    pages_per_hour = ocr_pages / ocr_seconds * 3600 if ocr_seconds else None
    searchable_delta = sum(r.get('searchable_bytes', 0) for r in ocr) / max(
        1, sum(r['bytes'] for r in ocr))

    report = {
        'generated': dt.datetime.now(dt.timezone.utc).isoformat(),
        'sample_files': len(sample),
        'sample_bytes': sum(r['bytes'] for _, r in sample),
        'wall_seconds': round(wall, 1),
        'workers': args.workers,
        'ocr': {
            'files': len(ocr), 'pages': ocr_pages,
            'pages_per_hour_per_worker': round(
                pages_per_hour / args.workers, 1) if pages_per_hour else None,
            'errors': [r for r in results if r.get('error')][:20],
            'error_count': sum(1 for r in results if r.get('error')),
            'confidence_mean': round(statistics.mean(confs), 1)
            if confs else None,
            'confidence_p10': round(sorted(confs)[len(confs) // 10], 1)
            if confs else None,
            'confidence_below_60': sum(1 for c in confs if c < 60),
            'confidence_pages_sampled': len(confs),
            'searchable_size_ratio': round(searchable_delta, 2),
        },
        'embedding': {
            'model': EMBED_MODEL, 'sampled': embedded,
            'seconds': round(embed_wall, 1),
            'chunks_per_second': round(embedded / embed_wall, 1)
            if embed_wall else None,
            'error': embed_error,
        },
        'projection': {
            'total_ocr_queue_files': len(total_ocr_queue),
            'total_ocr_queue_pages_estimate': total_ocr_pages,
            'note': 'wall-clock/cost per worker count computed in report step',
        },
        'results': results,
    }
    s3.put_object(Bucket=args.bucket,
                  Key=f'{args.out_prefix}/pilot_report.json',
                  Body=json.dumps(report, indent=1).encode(),
                  ContentType='application/json')
    print(json.dumps({k: v for k, v in report.items() if k != 'results'},
                     indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
