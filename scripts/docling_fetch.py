#!/usr/bin/env python3
"""PDF → разметка Docling (JSON) через сервис docling (docling-serve) — кусками, с кэшем.

Книгу отправляем кусками по --chunk страниц (асинхронная задача + опрос статуса): видно ход работы,
а после обрыва готовые куски берутся из кэша (--cache). Куски склеиваются в один DoclingDocument:
ссылки #/texts/N, #/groups/N, … следующих кусков сдвигаются, дети body/furniture дописываются по порядку.
В конце сервис выгружает модели (/v1/clear/converters), чтобы не держать память во время перевода.

Использование:
  python docling_fetch.py --pdf book.pdf --out book.json [--url http://docling:5001]
                          [--pages 170-229] [--chunk 50] [--cache dir]
  python docling_fetch.py --merge a.json b.json … --out book.json      # только склейка (проверка)
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.request, uuid

KINDS = ('texts', 'pictures', 'tables', 'groups', 'key_value_items', 'form_items', 'field_regions', 'field_items')
REF = re.compile(r'^#/(%s)/(\d+)$' % '|'.join(KINDS))
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # docling — сосед по compose, без прокси
OPTIONS = [('to_formats', 'json'), ('do_ocr', 'false'), ('force_ocr', 'false'), ('table_mode', 'accurate'),
           ('image_export_mode', 'placeholder'), ('include_images', 'false'), ('abort_on_error', 'false')]


def log(*a):
    print('[%s]    docling:' % time.strftime('%H:%M:%S'), *a, flush=True)


# ---------- склейка ----------
def shift(o, off):
    if isinstance(o, dict):
        return {k: (REF.sub(lambda m: '#/%s/%d' % (m.group(1), int(m.group(2)) + off[m.group(1)]), v)
                    if k in ('$ref', 'self_ref') and isinstance(v, str) else shift(v, off))
                for k, v in o.items()}
    if isinstance(o, list):
        return [shift(x, off) for x in o]
    return o


def merge(docs):
    base = json.loads(json.dumps(docs[0]))
    for d in docs[1:]:
        off = {k: len(base.get(k) or []) for k in KINDS}
        d = shift(d, off)
        for k in KINDS:
            if d.get(k):
                base.setdefault(k, []).extend(d[k])
        for top in ('body', 'furniture'):
            if d.get(top):
                base.setdefault(top, {'self_ref': '#/' + top, 'children': []})
                base[top].setdefault('children', []).extend(d[top].get('children', []))
        base.setdefault('pages', {}).update(d.get('pages') or {})
    return base


# ---------- сервис ----------
def request(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method='POST' if data is not None else 'GET')
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_ready(url, seconds):
    t0 = time.time()
    while True:
        try:
            with opener.open(url + '/health', timeout=5) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        if time.time() - t0 > seconds:
            sys.exit('Сервис docling не отвечает (%s). Проверь: docker.exe compose logs docling' % url)
        time.sleep(3)


def convert(url, pdf, a, b):
    bnd = uuid.uuid4().hex
    parts = []
    for k, v in OPTIONS + [('page_range', str(a)), ('page_range', str(b))]:
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (bnd, k, v)).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="files"; filename="%s"\r\n'
                  'Content-Type: application/pdf\r\n\r\n' % (bnd, os.path.basename(pdf))).encode())
    parts.append(open(pdf, 'rb').read())
    parts.append(('\r\n--%s--\r\n' % bnd).encode())
    task = request(url + '/v1/convert/file/async', b''.join(parts),
                   {'Content-Type': 'multipart/form-data; boundary=' + bnd}, timeout=300)
    tid = task['task_id']
    while task.get('task_status') not in ('success', 'failure', 'revoked'):
        time.sleep(float(os.environ.get("DOCLING_POLL", 5)))
        task = request(url + '/v1/status/poll/' + tid, timeout=60)
    if task['task_status'] != 'success':
        sys.exit('Docling: задача %s завершилась со статусом %s (стр. %d–%d)' % (tid, task['task_status'], a, b))
    res = request(url + '/v1/result/' + tid, timeout=300)
    doc = (res.get('document') or {}).get('json_content')
    if not doc:
        sys.exit('Docling не вернул JSON для стр. %d–%d: %s' % (a, b, res.get('errors')))
    if res.get('status') != 'success':
        log('стр. %d–%d: статус %s, ошибки: %s' % (a, b, res.get('status'), res.get('errors')))
    return doc


def page_count(pdf):
    import pdfplumber
    with pdfplumber.open(pdf) as p:
        return len(p.pages)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pdf')
    ap.add_argument('--out', required=True)
    ap.add_argument('--url', default=os.environ.get('DOCLING_URL', 'http://docling:5001'))
    ap.add_argument('--pages', help='A-B (по умолчанию вся книга)')
    ap.add_argument('--chunk', type=int, default=50)
    ap.add_argument('--cache', help='папка для готовых кусков')
    ap.add_argument('--merge', nargs='+', help='только склеить готовые JSON')
    a = ap.parse_args()

    if a.merge:
        docs = [json.load(open(f, encoding='utf-8')) for f in a.merge]
        json.dump(merge(docs), open(a.out, 'w', encoding='utf-8'), ensure_ascii=False)
        return

    n = page_count(a.pdf)
    lo, hi = (map(int, a.pages.split('-')) if a.pages else (1, n))
    hi = min(hi, n)
    cache = a.cache or os.path.dirname(os.path.abspath(a.out))
    os.makedirs(cache, exist_ok=True)
    ranges = [(s, min(s + a.chunk - 1, hi)) for s in range(lo, hi + 1, a.chunk)]
    todo = [r for r in ranges if not os.path.exists(os.path.join(cache, 'p%04d-%04d.json' % r))]
    log('страницы %d–%d, кусков %d (готово раньше: %d)' % (lo, hi, len(ranges), len(ranges) - len(todo)))
    if todo:
        wait_ready(a.url, 600)
        t0 = time.time()
        for i, (s, e) in enumerate(todo, 1):
            t = time.time()
            doc = convert(a.url, a.pdf, s, e)
            path = os.path.join(cache, 'p%04d-%04d.json' % (s, e))
            json.dump(doc, open(path + '.tmp', 'w', encoding='utf-8'), ensure_ascii=False)
            os.replace(path + '.tmp', path)
            left = (time.time() - t0) / i * (len(todo) - i)
            log('стр. %d–%d готовы за %d с (%d/%d, осталось ~%d мин)' % (s, e, time.time() - t, i, len(todo), left // 60))
        try:
            request(a.url + '/v1/clear/converters', timeout=60)
            log('модели Docling выгружены из памяти')
        except Exception as ex:          # не критично
            log('не удалось выгрузить модели: %s' % ex)
    docs = [json.load(open(os.path.join(cache, 'p%04d-%04d.json' % r), encoding='utf-8')) for r in ranges]
    json.dump(merge(docs), open(a.out, 'w', encoding='utf-8'), ensure_ascii=False)
    log('разметка: %s (%d стр.)' % (a.out, hi - lo + 1))


if __name__ == '__main__':
    main()
