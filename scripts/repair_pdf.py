#!/usr/bin/env python3
"""Ремонт перевода pdf2zh: повреждённые страницы переводятся заново короткими прогонами,
для каждой страницы берётся лучшая версия, а если код так и не восстановился —
после страницы вставляется английский оригинал с подписью.

Раунд 1: подряд идущие плохие страницы группами по 3, раунд 2: по одной (без кэша).
Короткие прогоны почти не дают «подмены кода» — она копится в длинном прогоне.

  python repair_pdf.py --orig work.pdf --tran pass0.pdf --out book_ru.pdf --code-font PAT
                       --config config.v3.toml --glossary g.csv --workdir DIR --report qa.txt
"""
import argparse, glob, os, re, subprocess, sys
import pdfplumber
import qa_pdf

LABEL = 'Английский оригинал предыдущей страницы — для сверки кода'
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'


def log(msg):
    print(msg, flush=True)


def groups(pages, size):
    out, cur = [], []
    for p in pages:
        if cur and (p != cur[-1] + 1 or len(cur) >= size):
            out.append(cur); cur = []
        cur.append(p)
    if cur:
        out.append(cur)
    return out


def translate(a, pages, outdir):
    os.makedirs(outdir, exist_ok=True)
    cmd = ['pdf2zh_next', '--config-file', a.config, a.orig, '--output', outdir,
           '--pages', ','.join(map(str, pages)), '--ignore-cache']
    if a.code_font:
        cmd += ['--formular-font-pattern', a.code_font]
    if a.glossary:
        cmd += ['--glossaries', a.glossary]
    r = subprocess.run(cmd)
    found = sorted(glob.glob(os.path.join(outdir, '*mono*.pdf')) or glob.glob(os.path.join(outdir, '*.pdf')),
                   key=os.path.getmtime)
    if r.returncode or not found:
        log('    ! прогон стр. %s не удался' % pages)
        return None
    return found[-1]


def label_page(a, p, outdir, size):
    """Страница оригинала p с серой подписью сверху."""
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    w, h = size
    lab = os.path.join(outdir, 'label_%d.pdf' % p)
    c = canvas.Canvas(lab, pagesize=(w, h))
    if os.path.exists(FONT):
        pdfmetrics.registerFont(TTFont('DV', FONT)); c.setFont('DV', 8); text = LABEL
    else:
        c.setFont('Helvetica', 8); text = 'English original of the previous page (for code reference)'
    c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawCentredString(w / 2, h - 12, text)
    c.showPage(); c.save()
    one = os.path.join(outdir, 'orig_%d.pdf' % p)
    out = os.path.join(outdir, 'orig_%d_l.pdf' % p)
    subprocess.run(['qpdf', a.orig, '--pages', a.orig, str(p), '--', one], check=True)
    subprocess.run(['qpdf', one, '--overlay', lab, '--', out], check=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    for k in ('orig', 'tran', 'out', 'config', 'workdir', 'report'):
        ap.add_argument('--' + k, required=True)
    ap.add_argument('--code-font', default='')
    ap.add_argument('--glossary', default='')
    ap.add_argument('--rounds', type=int, default=2)
    a = ap.parse_args()
    os.makedirs(a.workdir, exist_ok=True)
    code_re = re.compile(a.code_font) if a.code_font else re.compile(r'$^')

    log('    Проверка перевода…')
    res0, foreign, orig = qa_pdf.check(a.orig, a.tran, a.code_font)
    before = qa_pdf.summary(res0, foreign)
    log('    ' + before.splitlines()[0])
    best = {p: (r, (a.tran, p)) for p, r in res0.items()}
    bad = [p for p, r in res0.items() if r['bad']]

    for rnd, size in ((1, 3), (2, 1))[:a.rounds]:
        if not bad:
            break
        gs = groups(bad, size)
        log('    Раунд %d: %d страниц, %d коротких прогонов' % (rnd, len(bad), len(gs)))
        for k, g in enumerate(gs, 1):
            log('    [%d/%d] стр. %s' % (k, len(gs), ','.join(map(str, g))))
            pdf = translate(a, g, os.path.join(a.workdir, 'r%d_%d' % (rnd, g[0])))
            if not pdf:
                continue
            info = qa_pdf.collect(pdf, code_re)
            if len(info) != len(g):
                log('    ! в результате %d стр. вместо %d — пропускаю' % (len(info), len(g)))
                continue
            for i, p in enumerate(g):
                r = qa_pdf.score_page(orig[p - 1], info[i], foreign)
                if r['score'] < best[p][0]['score']:
                    best[p] = (r, (pdf, i + 1))
        bad = [p for p in bad if best[p][0]['bad']]

    # сборка: лучшая версия каждой страницы + оригинал после неисправимых
    sizes = {}
    with pdfplumber.open(a.orig) as pdf:
        for p in best:
            if best[p][0]['severe']:
                sizes[p] = (float(pdf.pages[p - 1].width), float(pdf.pages[p - 1].height))
    args, inserted, replaced = [], [], []
    for p in sorted(best):
        src, n = best[p][1]
        if src != a.tran:
            replaced.append(p)
        if args and args[-2] == src and re.fullmatch(r'\d+(-\d+)?', args[-1]) and int(args[-1].split('-')[-1]) == n - 1:
            args[-1] = '%s-%d' % (args[-1].split('-')[0], n)
        else:
            args += [src, str(n)]
        if p in sizes:
            args += [label_page(a, p, a.workdir, sizes[p]), '1']
            inserted.append(p)
    subprocess.run(['qpdf', a.tran, '--pages'] + args + ['--', a.out], check=True)

    after = {p: best[p][0] for p in best}
    minor = [p for p, r in after.items() if r['bad'] and p not in inserted]
    rep = ['ОТЧЁТ ПО ПЕРЕВОДУ PDF', '',
           'До ремонта: ' + before.splitlines()[0],
           'Заменено страниц лучшими версиями: %d %s' % (len(replaced), replaced[:40]),
           'Вставлены английские оригиналы после стр. (код не восстановился): %d %s' % (len(inserted), inserted),
           'Остались мелкие дефекты (например, «и» в строках кода): %d %s' % (len(minor), minor[:40]),
           '', 'Подробности до ремонта:', before]
    open(a.report, 'w', encoding='utf-8').write('\n'.join(rep) + '\n')
    log('\n'.join(rep[:5]))


if __name__ == '__main__':
    main()
