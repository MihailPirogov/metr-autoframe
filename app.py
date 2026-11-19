"""МЭТР FM — MetrAutoFrame: автокадрирование гостей в vMix.
Шаблоны ищутся по имени входа, окна проверяются перед отправкой, кадрирование можно отменить.
"""
import os, sys, re, json, threading, queue, tempfile, traceback, datetime, time
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np, cv2
from PIL import Image, ImageTk
import core

APPDIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CFG = os.path.join(APPDIR, "MetrAutoFrame.json")
DEFAULT = json.load(open(core.res("config.json"), encoding="utf-8"))
FRAME_RE = r"рамк|frame|okna|шаблон"
FLIP_TYPES = ("VLC", "Stream")
TFILE = os.path.join(APPDIR, "шаблоны.txt")
THEAD = """# МЭТР FM — список шаблонов для MetrAutoFrame
# Формат строки:   Отображаемое имя = название входа vMix  (или номер)
# Примеры:         Двое в студии = Двое
#                  Выездной эфир = 10
# Поиск входа — сначала точное название, затем без учёта регистра, затем номер.
# Если номер указывает на вход без камер, программа ищет вход по имени слева.
# Строки с # — комментарии, пустые строки не учитываются.
# Порядок строк = порядок в списке «Шаблон».
# Адрес vMix (необязательно):  vMix = 192.168.0.100
# Программа перечитывает файл сама, перезапуск не нужен.
"""

def read_tfile():
    """-> (ip or None, [(name, ref)]) where ref is int or str. None if no file."""
    if not os.path.exists(TFILE): return None
    raw = open(TFILE, "rb").read()
    try: txt = raw.decode("utf-8-sig")
    except UnicodeDecodeError: txt = raw.decode("cp1251", "replace")
    ip, ent = None, []
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = (x.strip() for x in line.rsplit("=", 1))
        if k.lower() in ("vmix", "ip", "ip vmix", "адрес vmix"): ip = v or None; continue
        if not v: continue
        ent.append((k, int(v) if v.isdigit() else v))
    return ip, ent

def write_tfile(ip, tpls):
    body = THEAD + f"\nvMix = {ip}\n\n" + "".join(f"{t['title']} = {t['title']}\n" for t in tpls)
    with open(TFILE, "w", encoding="utf-8-sig") as fh: fh.write(body)

def resolve_entry(name, ref, inputs, tpls):
    """Найти шаблон-камеры для строки шаблоны.txt.
    name — подпись слева, ref — название или номер входа справа.
    Порядок поиска: точное совпадение названия (важно: «Трое» и «трое» — разные входы),
    затем без учёта регистра, затем номер. Если номер устарел и вход без камер —
    берём вход по имени слева. Возвращает (шаблон, номер, предупреждение)."""
    by_num = {int(i.get("number")): i for i in inputs}
    tn = {t["number"]: t for t in tpls}
    by_title = {}
    for i in inputs:
        by_title.setdefault((i.get("title") or "").strip().lower(), []).append(i)

    def tpl_of(inp):
        return tn.get(int(inp.get("number")))

    if isinstance(ref, int):
        inp = by_num.get(ref)
        if inp is None:
            return None, ref, f"нет входа {ref}"
        t = tn.get(ref)
        if t is not None:
            return t, ref, None
        hits = by_title.get((name or "").strip().lower(), [])
        for h in hits:
            t2 = tpl_of(h)
            if t2 is not None:
                return t2, t2["number"], f"вход {ref} без камер, взят «{t2['title']}» (вход {t2['number']})"
        return None, ref, "нет слоёв-камер"

    want = (ref or "").strip()
    exact = [i for i in inputs if (i.get("title") or "") == want]
    ci = by_title.get(want.lower(), [])
    hits = exact or ci
    if not hits:
        return None, None, f"нет входа «{ref}»"
    with_cam = [h for h in hits if tpl_of(h) is not None]
    chosen = (with_cam or hits)[0]
    t = tpl_of(chosen)
    n = int(chosen.get("number"))
    warn = None
    if not exact and ci:
        warn = f"вход «{chosen.get('title')}» ({n}), регистр не совпал"
    if len(with_cam) > 1:
        warn = f"несколько входов «{ref}», взят вход {n}"
    if t is None:
        return None, n, "нет слоёв-камер"
    return t, n, warn

def parse_active(st):
    """Номер входа в Program из XML vMix (<active>).
    Принимает цифры с пробелами или ключ входа, если vMix вернул key вместо номера."""
    raw = (st.findtext("active") or "").strip()
    if raw.isdigit():
        return int(raw)
    for i in list(st.find("inputs") or []):
        if (i.get("key") or "") == raw and (i.get("number") or "").isdigit():
            return int(i.get("number"))
    return None

def load_cfg():

    c = dict(DEFAULT)
    if os.path.exists(CFG): c.update(json.load(open(CFG, encoding="utf-8")))
    c.setdefault("vmix_ip", "192.168.0.100"); c.setdefault("fit_all", True); c.setdefault("dp_url_port", 8098)
    return c

class App:
    def __init__(s, root):
        s.root = root; s.cfg = load_cfg(); root.title("MetrAutoFrame — МЭТР FM")
        try: root.iconbitmap(default=core.res("MetrAutoFrame.ico"))
        except Exception: pass
        s.q = queue.Queue(); s.tpls = []; s.last = None; s._deb = None; s.busy = False; s.use_dp = False
        s.tmp = os.path.join(tempfile.gettempdir(), f"metr_af21_{os.getpid()}"); os.makedirs(s.tmp, exist_ok=True)
        root.geometry(s.cfg.get("geometry", "1240x760")); root.minsize(640, 420)
        root.protocol("WM_DELETE_WINDOW", s.close)
        f = ttk.Frame(root, padding=8); f.pack(fill="both", expand=True); s._full = None; s._rdeb = None
        r = ttk.Frame(f); r.pack(fill="x")
        ttk.Label(r, text="IP vMix:").pack(side="left"); s.ip = tk.StringVar(value=s.cfg["vmix_ip"])
        e = ttk.Entry(r, textvariable=s.ip, width=16); e.pack(side="left", padx=4); e.bind("<Return>", lambda ev: s.go(s.refresh))
        ttk.Label(r, text="   Шаблон:").pack(side="left"); s.tplv = tk.StringVar()
        s.box = ttk.Combobox(r, textvariable=s.tplv, width=44, height=30, state="readonly"); s.box.pack(side="left", padx=4)
        s.box.bind("<<ComboboxSelected>>", lambda ev: (s.save(), s.go(s.refresh)))
        ttk.Button(r, text="⟳", width=3, command=lambda: s.go(s.refresh)).pack(side="left")
        s.onair = tk.Label(r, text="", font=("Segoe UI", 12, "bold"), fg="white", bg="#d0021b", padx=8); s.onair_shown = False
        r2 = ttk.Frame(f); r2.pack(fill="x", pady=8)
        ttk.Label(r2, text="Крупность:  мельче").pack(side="left")
        s.size = tk.DoubleVar(value=float(s.cfg.get("frame_size", 3.0)))
        tk.Scale(r2, from_=float(s.cfg.get("frame_size_max", 8.0)), to=float(s.cfg.get("frame_size_min", 1.3)), resolution=0.05,
                 orient="horizontal", length=200, variable=s.size, showvalue=True).pack(side="left", fill="x", expand=True)
        ttk.Label(r2, text="крупнее").pack(side="left")
        s.size.trace_add("write", lambda *a: s.on_size())
        st = ttk.Style(); st.configure("Big.TButton", font=("Segoe UI", 13, "bold"), padding=6)
        rowb = ttk.Frame(f); rowb.pack(fill="x")
        s.btn = ttk.Button(rowb, text="Кадрировать", style="Big.TButton", command=s.countdown); s.btn.pack(side="left", fill="x", expand=True)
        s.undobtn = ttk.Button(rowb, text="Отменить", command=s.undo); s.undobtn.state(["disabled"]); s.undobtn.pack(side="left", padx=(8, 0))
        s._undo = None
        s.status = tk.StringVar(value="выберите шаблон и нажмите «Кадрировать»")
        s.stl = ttk.Label(f, textvariable=s.status, foreground="#1a4fa0", wraplength=900, justify="left"); s.stl.pack(fill="x", pady=4)
        ttk.Label(f, text="© Михаил Пирогов · PirogovLab, 2026", foreground="#888888", font=("Segoe UI", 8)).pack(side="bottom", anchor="e")
        s.pv = ttk.Label(f, anchor="center"); s.pv.pack(fill="both", expand=True)
        s.pv.bind("<Configure>", lambda e: s._resched()); root.bind("<Configure>", lambda e: s.stl.configure(wraplength=max(300, root.winfo_width() - 30)) if e.widget is root else None)
        s.cd = tk.Label(root, text="", font=("Segoe UI", 220, "bold"), fg="#ffffff", bg="#c0392b")
        root.after(100, s._pump); root.after(500, lambda: s.go(s.refresh)); s.labmap = {}
        s._mt = os.path.getmtime(TFILE) if os.path.exists(TFILE) else None; root.after(2000, s._watch)

    # ---- infra ----
    def ui(s, fn): s.q.put(fn)
    def _pump(s):
        try:
            while True: s.q.get_nowait()()
        except queue.Empty: pass
        except Exception: s.log(traceback.format_exc())
        s.root.after(100, s._pump)
    def log(s, m):
        line = datetime.datetime.now().strftime("%H:%M:%S ") + m
        try:
            with open(os.path.join(APPDIR, "autoframe.log"), "a", encoding="utf-8") as fh: fh.write(line + "\n")
        except OSError: pass
    def say(s, m): s.log(m); s.ui(lambda: s.status.set(m))
    def save(s):
        if threading.current_thread() is threading.main_thread():
            s.cfg.update(vmix_ip=s.ip.get().strip() or "192.168.0.100", frame_size=round(float(s.size.get()), 2))
            lab = s.tplv.get(); n = getattr(s, "labmap", {}).get(lab)
            if n is None:
                m = re.match(r"(\d+)", lab); n = int(m.group(1)) if m else None
            if n is not None: s.cfg["template_number"] = n
        try: json.dump(s.cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except OSError: pass
    def go(s, fn):
        if s.busy: return
        s.save(); s.busy = True; s.btn.state(["disabled"])
        def w():
            try: fn()
            except Exception as e:
                msg = "ошибка: " + str(e)
                tb = traceback.format_exc(); s.log("ОШИБКА: " + tb); s.ui(lambda m=msg: s.status.set(m))
            finally:
                s.busy = False; s.ui(lambda: s.btn.state(["!disabled"]))
        threading.Thread(target=w, daemon=True).start()

    def countdown(s):
        if s.busy or getattr(s, "_cd", False): return
        s._cd = True; s.btn.state(["disabled"]); n = int(s.cfg.get("countdown_sec", 3))
        try: s.root.attributes("-topmost", True); s.root.lift()
        except Exception: pass
        def tick(k):
            if k > 0:
                fs = max(48, min(s.root.winfo_width(), s.root.winfo_height()) // 3)
                s.cd.configure(text=str(k), font=("Segoe UI", fs, "bold")); s.cd.place(relx=0.5, rely=0.55, anchor="center"); s.cd.lift(); s.root.after(1000, lambda: tick(k - 1))
            else:
                s.cd.place_forget(); s._cd = False
                try: s.root.attributes("-topmost", False)
                except Exception: pass
                s.btn.state(["!disabled"]); s.go(s.frame)
        tick(n)

    def _watch(s):
        """Раз в 2 секунды: если изменился шаблоны.txt — перечитать список;
        параллельно опросить Program и обновить метку «● ЭФИР», не трогая камеры."""
        try:
            mt = os.path.getmtime(TFILE) if os.path.exists(TFILE) else None
            if mt != s._mt: s._mt = mt; s.log("шаблоны.txt изменён — перечитываю"); s.go(s.refresh)
        except Exception: pass
        if not s.busy and not getattr(s, "_cd", False) and not getattr(s, "_pgm_busy", False):
            s._pgm_busy = True
            threading.Thread(target=s._poll_pgm, daemon=True).start()
        s.root.after(2000, s._watch)

    def _poll_pgm(s):
        """Опрос Program: если активный вход сменился, пересобрать подписи «● ЭФИР».
        Кадры камер заново не снимаются — только метки в списке шаблонов."""
        try:
            st = s.vm().state()
            n = parse_active(st)
            if n == getattr(s, "pgm", None) and getattr(s, "_labels_cache", None):
                return
            prev = getattr(s, "pgm", None)
            s.pgm = n
            if n != prev:
                s.log(f"в эфире вход {n}")
            s._apply_onair_labels(st)
        except Exception as e:
            s.log(f"опрос эфира: {e}")
        finally:
            s._pgm_busy = False

    def _apply_onair_labels(s, st):
        """Обновить список шаблонов и красный значок «ЭФИР», сохранив выбранный номер."""
        tpls = getattr(s, "tpls", None)
        if not tpls: return
        tf = read_tfile()
        labels, labmap = s._labels(st, tpls, tf)
        want = getattr(s, "labmap", {}).get(s.tplv.get()) if hasattr(s, "tplv") else s.cfg.get("template_number")
        if want is None: want = s.cfg.get("template_number")
        sel = next((l for l in labels if labmap.get(l) == want), labels[0] if labels else "")
        s.labmap = labmap; s._labels_cache = labels
        def upd():
            s.box.configure(values=labels)
            s.tplv.set(sel)
            if want is not None and want == s.pgm:
                s.onair.configure(text="ЭФИР"); s.onair.pack(side="left", padx=6)
            else:
                s.onair.pack_forget()
        s.ui(upd)

    def _labels(s, st, tpls, tf):
        """Подписи списка шаблонов: имя из шаблоны.txt плюс «● ЭФИР», если номер совпал с Program."""
        labels, labmap = [], {}
        if tf and tf[1]:
            for name, ref in tf[1]:
                t0, n, warn = resolve_entry(name, ref, list(st.find("inputs")), tpls)
                lab = name
                if warn: lab += "  ⚠ " + warn
                elif t0 is None and n is None: lab += "  ⚠ нет входа"
                elif t0 is None: lab += "  · нет слоёв-камер"
                if n is not None and n == s.pgm: lab += "  ● ЭФИР"
                while lab in labmap: lab += " "
                labels.append(lab); labmap[lab] = n if n is not None else -1
        else:
            for t in tpls:
                lab = f"{t['number']} — {t['title']} ({t['n']} окн.{'' if t.get('framed') else ', без рамок'})"
                if t["number"] == s.pgm: lab += "  ● ЭФИР"
                labels.append(lab); labmap[lab] = t["number"]
        return labels, labmap

    def _resched(s):
        if s._rdeb: s.root.after_cancel(s._rdeb)
        s._rdeb = s.root.after(150, s.render)
    def render(s):
        s._rdeb = None
        if s._full is None: return
        W_, H_ = max(50, s.pv.winfo_width() - 4), max(50, s.pv.winfo_height() - 4)
        h, w = s._full.shape[:2]; k = min(W_ / w, H_ / h)
        img = cv2.resize(s._full, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
        im = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))); s.pv.configure(image=im); s._im = im
    def close(s):
        try: s.cfg["geometry"] = s.root.geometry(); s.save()
        except Exception: pass
        s.root.destroy()

    # ---- vMix / pictures ----
    def remote(s): return not core.is_local(s.cfg["vmix_ip"])
    def vm(s):
        v = core.VMix(core.api_url(s.cfg["vmix_ip"]), s.log); v.remote = s.remote(); v.allow_snap = False; return v
    def dp(s): return f"http://{s.cfg['vmix_ip'].split(':')[0]}:{s.cfg.get('dp_url_port', 8098)}"
    def dp_ok(s):
        try: core.urllib.request.urlopen(s.dp() + "/thumbnails", timeout=4).read(1); return True
        except Exception: return False
    def picture(s, vm, num, key, regen=True):
        if s.remote() or s.use_dp:
            if regen:
                try: core.urllib.request.urlopen(f"{s.dp()}/regen/{num}", timeout=20).read(); time.sleep(0.4)
                except Exception as e: s.log(f"regen {num}: {e}")
            return core.dp_thumb(s.dp(), key)
        return vm.snapshot(str(num), os.path.join(s.tmp, f"in{num}.png"))

    def refresh(s):
        tf = read_tfile()
        if tf and tf[0] and tf[0] != s.cfg.get("vmix_ip"):
            s.cfg["vmix_ip"] = tf[0]; s.ui(lambda: s.ip.set(tf[0]))
        vm = s.vm(); st = vm.state(); s.use_dp = s.remote() or s.dp_ok(); s.applied = False
        by_key = {i.get("key"): i for i in st.find("inputs")}
        tpls = core.discover(st, s.cfg)   # v24: every input with camera layers, frames or not
        for t in tpls:
            inp = by_key[t["key"]]
            t["framed"] = any((by_key.get(o.get("key")) is not None
                    and by_key[o.get("key")].get("type") in ("Image", "ImageSequence")
                    and re.search(FRAME_RE, by_key[o.get("key")].get("title") or "", re.I)) for o in inp.findall("overlay"))
        tpls.sort(key=lambda t: t["number"]); s.tpls = tpls
        tnums = {t["number"] for t in tpls}
        others = [i for i in st.find("inputs") if int(i.get("number")) not in tnums]
        s.pgm = parse_active(st)
        labels = [f"{t['number']} — {t['title']} ({t['n']} окн.{'' if t['framed'] else ', без рамок'})" + ("  ● ЭФИР" if t["number"] == s.pgm else "") for t in tpls]
        labels += [f"{i.get('number')} — {i.get('title')}  · нет слоёв-камер" + ("  ● ЭФИР" if (i.get("number") or "").isdigit() and int(i.get("number")) == s.pgm else "") for i in others]
        s.labmap = {l: int(l.split(" — ")[0]) for l in labels}
        if tf is None:
            try: write_tfile(s.cfg["vmix_ip"], tpls); s._mt = os.path.getmtime(TFILE); s.log("создан шаблоны.txt"); tf = read_tfile()
            except OSError as e: s.log(f"шаблоны.txt не создан: {e}")
        s.tpl_warn = {}
        if tf and tf[1]:
            for name, ref in tf[1]:
                t0, n, warn = resolve_entry(name, ref, list(st.find("inputs")), tpls)
                if warn: s.log(f"шаблон «{name}»: {warn}")
        labels, s.labmap = s._labels(st, tpls, tf); s._labels_cache = labels
        want = s.cfg.get("template_number")
        sel = next((l for l in labels if s.labmap.get(l) == want), labels[0] if labels else "")
        def upd():
            s.box.configure(values=labels); s.tplv.set(sel)
            tt = s.cur(sel)
            if "● ЭФИР" in sel: s.onair.configure(text="ЭФИР"); s.onair.pack(side="left", padx=6)
            else: s.onair.pack_forget()
        s.ui(upd)
        t = s.cur(sel)
        if t is None: s.say("в этом входе нет слоёв с камерами — выберите шаблон с камерами" if s.tpls else "в vMix не найдено входов со слоями-камерами"); s._full = None; s.ui(lambda: s.pv.configure(image="")); return
        s.say(f"vMix {s.cfg['vmix_ip']}: шаблонов {len(tpls)}; «{t['title']}» — камеры {t['cams']}, окон {t['n']}" + (" · картинки с DP Thumbnail Server" if s.use_dp else ""))
        try: s.prepare(vm, st, t, regen=False); s.place(vm, send=False)
        except Exception as e: s.log(f"превью камер: {e}")

    def cur(s, lab=None):
        lab = lab or s.tplv.get(); n = getattr(s, "labmap", {}).get(lab)
        if n is None:
            m = re.match(r"(\d+)", lab or ""); n = int(m.group(1)) if m else None
        if n is None: return None
        return next((t for t in s.tpls if t["number"] == n), None)   # None = input without camera layers

    def windows(s, vm, st, t):
        """Окна шаблона, в порядке приоритета:
        1) прозрачные отверстия PNG-рамки (вход Image или ImageSequence),
        2) кеш окна.json по имени видео-оформления,
        3) альфа-канал .mov (через ffmpeg, один раз),
        4) геометрия слоёв-камер.
        Перед использованием окна проверяет check_windows: слишком маленькие,
        вне кадра или сильно пересекающиеся — кадрирование отменяется."""
        cache = os.path.join(APPDIR, "окна.json")
        n = t["n"]
        # 1. frame PNG / ImageSequence
        try:
            if s.use_dp: wins = core.dp_frame_windows(s.dp(), st, t["key"], FRAME_RE)
            else:
                wins = []
                by_key = {i.get("key"): i for i in st.find("inputs")}
                for o in by_key[t["key"]].findall("overlay"):
                    src = by_key.get(o.get("key"))
                    if src is not None and src.get("type") in ("Image", "ImageSequence") and re.search(FRAME_RE, src.get("title") or "", re.I):
                        wins = core.frame_windows(core.snapshot_rgba(vm, o.get("key"), os.path.join(s.tmp, "frame.png"))); break
        except Exception as e: s.log(f"рамки: {e}"); wins = []
        if len(wins) == n and not core.check_windows(wins):
            return sorted(wins), "по рамкам (PNG)"
        # 2. окна.json by decoration video title
        try:
            jw = core.json_windows(st, t["key"], cache, s.log)
            if len(jw) == n and not core.check_windows(jw):
                return sorted(jw), "по окна.json"
        except Exception as e: s.log(f"окна.json: {e}")
        # 3. .mov alpha via ffmpeg (do not re-read json)
        try:
            dirs = [APPDIR] + list(s.cfg.get("media_dirs") or [r"C:\МЭТР FM\vMix"])
            vw = core.video_alpha_windows(st, t["key"], dirs, s.cfg.get("ffmpeg", "ffmpeg"), s.tmp, s.log, cache, use_cache=False)
            if len(vw) == n and not core.check_windows(vw):
                return sorted(vw), "по альфа-каналу видео"
        except Exception as e: s.log(f"альфа-окна: {e}")
        # 4. camera layer geometry
        layer = [t["rects"][i] for i in sorted(range(n), key=lambda i: t["rects"][i][0])]
        return layer, "по слоям камер"

    # ---- main action ----
    def _layer_snapshot(s, vm, key, layers):
        """Снимок текущих zoom/pan/crop слоёв шаблона — для кнопки «Отменить»."""
        inp = next((i for i in vm.state().find("inputs") if i.get("key") == key), None)
        if inp is None: return None
        got = {int(o.get("index")) + 1: o for o in inp.findall("overlay")}
        snap = []
        for Ln in layers:
            o = got.get(Ln)
            if o is None: continue
            pos = o.find("position") or {}; crop = o.find("crop")
            z = (pos.get("zoomX") if hasattr(pos, "get") else None) or "1"
            px = (pos.get("panX") if hasattr(pos, "get") else None) or "0"
            py = (pos.get("panY") if hasattr(pos, "get") else None) or "0"
            if crop is not None and crop.get("X1") is not None:
                cr = f"{crop.get('X1')},{crop.get('Y1')},{crop.get('X2')},{crop.get('Y2')}"
            else:
                cr = "0,0,1,1"
            snap.append((Ln, z, px, py, cr))
        return snap

    def undo(s):
        """Вернуть слоям шаблона zoom/pan/crop, которые были до последнего кадрирования."""
        if s.busy or not getattr(s, "_undo", None): return
        s.go(s._undo_run)

    def _undo_run(s):
        u = s._undo
        if not u: s.say("нечего отменять"); return
        vm = s.vm()
        for Ln, z, px, py, cr in u["snap"]:
            vm.fn(f"SetLayer{Ln}Zoom", Input=u["key"], Value=str(z))
            vm.fn(f"SetLayer{Ln}PanX", Input=u["key"], Value=str(px))
            vm.fn(f"SetLayer{Ln}PanY", Input=u["key"], Value=str(py))
            vm.fn(f"SetLayer{Ln}Crop", Input=u["key"], Value=str(cr))
        s.applied = False
        s.ui(lambda: s.undobtn.state(["disabled"]))
        s.say(f"отменено: восстановлены слои «{u['title']}»")
        s._undo = None

    def frame(s):
        vm = s.vm(); st = vm.state()
        if not s.tpls: s.refresh(); st = vm.state()
        t = s.cur()
        if t is None: s.say("нет шаблона"); return
        s.prepare(vm, st, t, regen=True); s.place(vm, verbose=True)

    def prepare(s, vm, st, t, regen=True):
        t = next((x for x in core.discover(st, s.cfg) if x["key"] == t["key"]), t)   # fresh layers
        order = sorted(range(t["n"]), key=lambda i: t["rects"][i][0])
        layers = [t["layers"][i] for i in order]; cams = [t["cams"][i] for i in order]
        rects, how = s.windows(vm, st, t)
        keys = {i.get("number"): i.get("key") for i in st.find("inputs")}; types = {i.get("number"): i.get("type") for i in st.find("inputs")}
        imgs = {}
        for c in sorted(set(cams)): imgs[c] = s.picture(vm, c, keys[str(c)], regen=regen)
        faces = {c: core.detect_faces(imgs[c], s.cfg.get("face_score", 0.6), s.log) for c in imgs}
        flips = {}
        if not s.remote():
            rot = core.preset_rotations(st); flips = {c: rot.get(keys[str(c)]) for c in cams if rot.get(keys[str(c)]) is not None}
        for c in cams: flips.setdefault(c, (True, True) if types.get(str(c)) in FLIP_TYPES else (False, False))
        s.last = dict(t=t, layers=layers, cams=cams, rects=rects, how=how, imgs=imgs, faces=faces, flips=flips)
        s.log(f"кадрировать «{t['title']}»: окна {how} {rects}, камеры {cams}, лица " + ", ".join(f"{c}: {len(faces[c])}" for c in imgs))

    def place(s, vm=None, verbose=False, send=True):
        L = s.last; vm = vm or s.vm(); cfg = dict(s.cfg); imgs, faces = L["imgs"], L["faces"]
        bad = core.check_windows(L["rects"])
        if bad:
            msg = "кадрирование отменено: " + "; ".join(bad) + " · окна " + L.get("how", "")
            if send: s.say(msg); s.applied = False
            else: s.say(f"«{L['t']['title']}»: окна {L.get('how','')} — {'; '.join(bad)}")
            return
        items, idx, empty_solo, vacant = [], [], [], []
        # Общая камера (несколько окон с одним входом, например окна 2 и 3 в «Трое»):
        # человека ставим в окно со своей стороны кадра; в пустое окно — свободную
        # часть этой же камеры в том же масштабе, без этого человека в кадре.
        # Если лиц нет — окна делят картинку поровну слева направо.
        for c in list(dict.fromkeys(L["cams"])):
            slots = [i for i, cc in enumerate(L["cams"]) if cc == c]
            nw = len(slots)
            fl = list(faces.get(c) or [])
            if nw > 1:
                fl = sorted(fl, key=lambda x: -x[2])[:nw]
            elif fl:
                mx = max(x[2] for x in fl)
                fl = [x for x in fl if x[2] >= float(s.cfg.get("group_min_face", 0.6)) * mx]
            if nw == 1:
                if not fl:
                    empty_solo.append(slots[0] + 1); continue
                f = core.group_face(fl) if (cfg.get("fit_all", True) and len(fl) > 1) else max(fl, key=lambda x: x[2])
                items.append((c, f, L["rects"][slots[0]])); idx.append(slots[0])
                continue
            img_w = imgs[c].shape[1] if c in imgs and imgs[c] is not None else core.W
            mapping = core.assign_shared(fl, nw, img_w)
            kind = "split" if not mapping else "empty"
            for k, i in enumerate(slots):
                if k in mapping:
                    items.append((c, mapping[k], L["rects"][i])); idx.append(i)
                else:
                    vacant.append((i, c, k, nw, kind))
        info = {}
        if items and cfg.get("fit_all", True):
            fw1 = {c: (sum(x[2] for x in faces[c]) / len(faces[c])) for c in imgs if faces[c]}
            smin = max(core.fit_size(f, fw1[c], imgs[c].shape[1], imgs[c].shape[0], w[2], w[3], cfg.get("eye_line", 0.42)) for c, f, w in items)
            cap = min(float(cfg.get("frame_size_max", 8.0)), max(core.whole_size(f, imgs[c].shape[1], imgs[c].shape[0], w[2], w[3]) for c, f, w in items))
            cfg["_s_min"] = min(smin, cap)
        crops, us = core.uniform_crops([(f, imgs[c].shape[1], imgs[c].shape[0], w[2], w[3]) for c, f, w in items], cfg, info) if items else ([], cfg["frame_size"])
        # Пустые окна общей камеры: кроп свободной зоны (тот же зум, что у занятого
        # окна этой камеры) либо равномерный разрез кадра, если лиц нет совсем.
        vac_by = {}
        for rec in vacant:
            vac_by.setdefault(rec[1], []).append(rec)
        vac_items, vac_crops, vac_idx = [], [], []
        for c, recs in vac_by.items():
            recs = sorted(recs, key=lambda r: r[2])
            h, w = imgs[c].shape[:2]
            occ = [(crops[n], items[n][2]) for n, (cc, f, win) in enumerate(items) if cc == c]
            if occ:
                ref = occ[0][0]
                for i, cc, k, nw, kind in recs:
                    win = L["rects"][i]
                    kasp = (win[2] / max(win[3], 1e-6)) * (w / core.W) / (h / core.H)
                    cw, ch = ref[3] * kasp, ref[3]
                    cr = core.empty_region_crop(w, h, cw, ch, k, nw, faces.get(c) or [], ref_crop=ref)
                    vac_items.append((c, None, win)); vac_crops.append(cr); vac_idx.append(i)
            else:
                wins = [(L["rects"][i][2], L["rects"][i][3]) for i, cc, k, nw, kind in recs]
                nw = recs[0][3]
                for (i, cc, k, nw, kind), cr in zip(recs, core.even_split_crops(w, h, wins, nw)):
                    vac_items.append((c, None, L["rects"][i])); vac_crops.append(cr); vac_idx.append(i)
        sent, warn, miss = [], [], {}
        warn += [f"в окне {n} нет человека" for n in empty_solo]
        warn += [f"в окне {i + 1} пустое место" for i in vac_idx]
        if send:
            snap = s._layer_snapshot(vm, L["t"]["key"], L["layers"])
            if snap:
                s._undo = dict(key=L["t"]["key"], title=L["t"]["title"], snap=snap)
        def _apply_crop(i, c, win, cr, check_miss=True):
            h, w = imgs[c].shape[:2]; lp = core.layer_params(cr, w, h, win)
            u0, v0, u1, v1 = lp["crop"]; fx, fy = L["flips"].get(c, (False, False))
            if fx: u0, u1 = 1 - u1, 1 - u0
            if fy: v0, v1 = 1 - v1, 1 - v0
            Ln = L["layers"][i]; inp = L["t"]["key"]
            if send: vm.fn(f"SetLayer{Ln}Zoom", Input=inp, Value=f"{lp['zoom']:.5f}"); vm.fn(f"SetLayer{Ln}PanX", Input=inp, Value=f"{lp['panx']:.5f}")
            if send: vm.fn(f"SetLayer{Ln}PanY", Input=inp, Value=f"{lp['pany']:.5f}"); vm.fn(f"SetLayer{Ln}Crop", Input=inp, Value=f"{u0:.5f},{v0:.5f},{u1:.5f},{v1:.5f}")
            sent.append((Ln, lp["zoom"], round(u0, 4)))
            if check_miss:
                ms = core.missing_sides(cr, w, h)
                if ms:
                    miss.setdefault(c, {}).update(ms)
                    warn.append(f"кам.{c}: не хватает картинки " + " и ".join(core.SIDE_RU[x] for x in "LRTB" if x in ms) + " → повернуть " + "/".join(core.MOVE_RU[x] for x in "LRTB" if x in ms))
        for n, (i, (c, f, win)) in enumerate(zip(idx, items)):
            _apply_crop(i, c, win, crops[n], check_miss=True)
        for n, (i, (c, f, win)) in enumerate(zip(vac_idx, vac_items)):
            _apply_crop(i, c, win, vac_crops[n], check_miss=False)
        show_items = items + vac_items
        show_crops = list(crops) + vac_crops
        if not send:
            s.say(f"«{L['t']['title']}»: камеры {L['cams']}, лица " + ", ".join(f"{c}: {len(faces[c])}" for c in imgs) + " — предпросмотр, нажмите «Кадрировать» · окна " + L["how"] + (" · " + "; ".join(warn) if warn else ""))
            s.show(None, L, show_crops, show_items, miss); return
        # readback
        ok = True
        try:
            inp = next(i for i in vm.state().find("inputs") if i.get("key") == L["t"]["key"])
            got = {int(o.get("index")) + 1: o for o in inp.findall("overlay")}
            for Ln, z, u0 in sent:
                p = got[Ln].find("position"); ok &= p is not None and abs(float(p.get("zoomX", 0)) - z) < 1e-3
        except Exception as e: ok = False; s.log(f"проверка: {e}")
        msg = (f"готово ✓ окон {len(sent)}/{len(L['cams'])}, крупность {us:.2f}" if ok else "⚠ vMix не подтвердил значения")
        if warn: msg += " · " + "; ".join(warn)
        msg += " · окна " + L["how"]
        s.applied = True
        s.ui(lambda: s.undobtn.state(["!disabled"]))
        s.say(msg)
        s.show(None, L, show_crops, show_items, miss)

    def on_size(s):
        if s._deb: s.root.after_cancel(s._deb)
        s._deb = s.root.after(350, s.apply_size)
    def apply_size(s):
        s._deb = None; s.save()
        if s.last and not s.busy: s.go(lambda: s.place(verbose=False, send=getattr(s, "applied", False)))

    # ---- preview: plan picture on top, cameras with faces (green) and crops (yellow) below ----
    def show(s, plan, L=None, crops=(), items=(), miss=None):
        rows = []; TW = 1600
        if L:
            tiles = []
            byc = {}
            for (c, f, w), cr in zip(items, crops): byc.setdefault(c, []).append(cr)
            for c, img in L["imgs"].items():
                v = img.copy(); th = max(3, v.shape[1] // 300); H_, W_ = v.shape[:2]
                for (x, y, w, h, _) in L["faces"][c]: cv2.rectangle(v, (int(x), int(y)), (int(x + w), int(y + h)), (0, 255, 0), th)
                for (l, t_, cw, ch) in byc.get(c, []): cv2.rectangle(v, (int(l), int(t_)), (int(l + cw), int(t_ + ch)), (0, 220, 255), th)
                e = max(10, W_ // 60)
                for sd in (miss or {}).get(c, {}):
                    if sd == "L": v[:, :e] = (0, 0, 255)
                    if sd == "R": v[:, W_ - e:] = (0, 0, 255)
                    if sd == "T": v[:e, :] = (0, 0, 255)
                    if sd == "B": v[H_ - e:, :] = (0, 0, 255)
                cv2.putText(v, f"вход {c}".encode("ascii", "ignore").decode() or str(c), (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 4)
                tw = TW // max(1, len(L["imgs"])) - 6; tiles.append(cv2.copyMakeBorder(cv2.resize(v, (tw, int(tw * 9 / 16))), 0, 0, 3, 3, cv2.BORDER_CONSTANT))
            row = np.hstack(tiles)
            s._cams = row
        if getattr(s, "_cams", None) is not None: rows.append(s._cams)
        if not rows: return
        full = np.vstack(rows)
        def upd(): s._full = full; s.render()
        s.ui(upd)

if __name__ == "__main__":
    root = tk.Tk()
    def fatal(*a):
        tb = "".join(traceback.format_exception(*a)) if len(a) == 3 else str(a)
        try: open(os.path.join(APPDIR, "autoframe.log"), "a", encoding="utf-8").write("СБОЙ v21:\n" + tb + "\n")
        except OSError: pass
    root.report_callback_exception = fatal
    App(root); root.mainloop()
