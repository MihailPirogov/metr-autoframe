"""МЭТР FM — MetrAutoFrame v21 (упрощённый): шаблон, IP vMix, крупность, превью, кнопка «Кадрировать»."""
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
# Формат строки:   Отображаемое имя = номер входа vMix
# Пример:          Двое в студии = 10
# Строки с # — комментарии, пустые строки не учитываются.
# Порядок строк = порядок в списке «Шаблон».
# Адрес vMix (необязательно):  vMix = 192.168.0.100
# Программа перечитывает файл сама, перезапуск не нужен.
"""

def read_tfile():
    """-> (ip or None, [(name, number)]) or None if no file."""
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
        ent.append((k, int(v) if v.isdigit() else None))
    return ip, ent

def write_tfile(ip, tpls):
    body = THEAD + f"\nvMix = {ip}\n\n" + "".join(f"{t['title']} = {t['number']}\n" for t in tpls)
    with open(TFILE, "w", encoding="utf-8-sig") as fh: fh.write(body)

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
        s.btn = ttk.Button(f, text="Кадрировать", style="Big.TButton", command=s.countdown); s.btn.pack(fill="x")
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
                tb = traceback.format_exc(); s.log("ОШИБКА: " + tb); s.ui(lambda: s.status.set("ошибка: " + str(e)))
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
        try:
            mt = os.path.getmtime(TFILE) if os.path.exists(TFILE) else None
            if mt != s._mt: s._mt = mt; s.log("шаблоны.txt изменён — перечитываю"); s.go(s.refresh)
        except Exception: pass
        s.root.after(2000, s._watch)

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
            t["framed"] = any((by_key.get(o.get("key")) is not None and by_key[o.get("key")].get("type") == "Image"
                    and re.search(FRAME_RE, by_key[o.get("key")].get("title") or "", re.I)) for o in inp.findall("overlay"))
        tpls.sort(key=lambda t: t["number"]); s.tpls = tpls
        tnums = {t["number"] for t in tpls}
        others = [i for i in st.find("inputs") if int(i.get("number")) not in tnums]
        pgm = st.findtext("active") or ""; s.pgm = int(pgm) if pgm.isdigit() else None
        labels = [f"{t['number']} — {t['title']} ({t['n']} окн.{'' if t['framed'] else ', без рамок'})" + ("  ● ЭФИР" if t["number"] == s.pgm else "") for t in tpls]
        labels += [f"{i.get('number')} — {i.get('title')}  · нет слоёв-камер" + ("  ● ЭФИР" if i.get("number") == pgm else "") for i in others]
        s.labmap = {l: int(l.split(" — ")[0]) for l in labels}
        if tf is None:
            try: write_tfile(s.cfg["vmix_ip"], tpls); s._mt = os.path.getmtime(TFILE); s.log("создан шаблоны.txt"); tf = read_tfile()
            except OSError as e: s.log(f"шаблоны.txt не создан: {e}")
        if tf and tf[1]:   # v25: admin list only
            nums = {int(i.get("number")): i for i in st.find("inputs")}; tn = {t["number"]: t for t in tpls}; labels = []; s.labmap = {}
            for name, n in tf[1]:
                lab = name
                if n is None or n not in nums: lab += f"  ⚠ нет входа {n if n is not None else ''}".rstrip()
                elif n not in tn: lab += "  · нет слоёв-камер"
                if n is not None and n == s.pgm: lab += "  ● ЭФИР"
                while lab in s.labmap: lab += " "
                labels.append(lab); s.labmap[lab] = n if (n in nums) else -1
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
        try:
            if s.use_dp: wins = core.dp_frame_windows(s.dp(), st, t["key"], FRAME_RE)
            else:
                wins = []
                by_key = {i.get("key"): i for i in st.find("inputs")}
                for o in by_key[t["key"]].findall("overlay"):
                    src = by_key.get(o.get("key"))
                    if src is not None and src.get("type") == "Image" and re.search(FRAME_RE, src.get("title") or "", re.I):
                        wins = core.frame_windows(core.snapshot_rgba(vm, o.get("key"), os.path.join(s.tmp, "frame.png"))); break
        except Exception as e: s.log(f"рамки: {e}"); wins = []
        if len(wins) != t["n"]:   # v26: decoration video with transparent windows (alpha channel)
            try:
                dirs = [APPDIR] + list(s.cfg.get("media_dirs") or [r"C:\МЭТР FM\vMix"])
                vw = core.video_alpha_windows(st, t["key"], dirs, s.cfg.get("ffmpeg", "ffmpeg"), s.tmp, s.log, os.path.join(APPDIR, "окна.json"))
                if len(vw) == t["n"]: return sorted(vw), "по альфа-каналу видео"
            except Exception as e: s.log(f"альфа-окна: {e}")
        if len(wins) == t["n"]: return sorted(wins), "по рамкам"
        return [t["rects"][i] for i in sorted(range(t["n"]), key=lambda i: t["rects"][i][0])], "по слоям (рамки не прочитаны)"

    # ---- main action ----
    def frame(s):
        vm = s.vm(); st = vm.state()
        if not s.tpls: s.refresh(); st = vm.state()
        t = s.cur()
        if t is None: s.say("нет шаблона"); return
        s.prepare(vm, st, t, regen=True); s.place(vm, verbose=True); s.applied = True

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
        items, idx = [], []
        for i, (c, win) in enumerate(zip(L["cams"], L["rects"])):
            fl = faces[c]
            if not fl: continue
            mx = max(x[2] for x in fl); fl = [x for x in fl if x[2] >= float(s.cfg.get("group_min_face", 0.6)) * mx]   # v22: ignore small/partial faces at the edge
            f = core.group_face(fl) if (cfg.get("fit_all", True) and len(fl) > 1 and L["cams"].count(c) == 1) else max(fl, key=lambda x: x[2])
            if L["cams"].count(c) > 1:   # one camera feeding several windows: k-th window gets k-th face from the left
                srt = sorted(fl, key=lambda x: x[0]); k = [j for j, cc in enumerate(L["cams"]) if cc == c].index(i)
                if k < len(srt): f = srt[k]
            items.append((c, f, win)); idx.append(i)
        info = {}
        if items and cfg.get("fit_all", True):
            fw1 = {c: (sum(x[2] for x in faces[c]) / len(faces[c])) for c in imgs if faces[c]}
            smin = max(core.fit_size(f, fw1[c], imgs[c].shape[1], imgs[c].shape[0], w[2], w[3], cfg.get("eye_line", 0.42)) for c, f, w in items)
            cap = min(float(cfg.get("frame_size_max", 8.0)), max(core.whole_size(f, imgs[c].shape[1], imgs[c].shape[0], w[2], w[3]) for c, f, w in items))
            cfg["_s_min"] = min(smin, cap)
        crops, us = core.uniform_crops([(f, imgs[c].shape[1], imgs[c].shape[0], w[2], w[3]) for c, f, w in items], cfg, info) if items else ([], cfg["frame_size"])
        sent, warn, miss = [], [], {}
        for n, (i, (c, f, win)) in enumerate(zip(idx, items)):
            h, w = imgs[c].shape[:2]; cr = crops[n]; lp = core.layer_params(cr, w, h, win)
            u0, v0, u1, v1 = lp["crop"]; fx, fy = L["flips"].get(c, (False, False))
            if fx: u0, u1 = 1 - u1, 1 - u0
            if fy: v0, v1 = 1 - v1, 1 - v0
            Ln = L["layers"][i]; inp = L["t"]["key"]
            if send: vm.fn(f"SetLayer{Ln}Zoom", Input=inp, Value=f"{lp['zoom']:.5f}"); vm.fn(f"SetLayer{Ln}PanX", Input=inp, Value=f"{lp['panx']:.5f}")
            if send: vm.fn(f"SetLayer{Ln}PanY", Input=inp, Value=f"{lp['pany']:.5f}"); vm.fn(f"SetLayer{Ln}Crop", Input=inp, Value=f"{u0:.5f},{v0:.5f},{u1:.5f},{v1:.5f}")
            sent.append((Ln, lp["zoom"], round(u0, 4)))
            ms = core.missing_sides(cr, w, h)
            if ms:
                miss.setdefault(c, {}).update(ms)
                warn.append(f"кам.{c}: не хватает картинки " + " и ".join(core.SIDE_RU[x] for x in "LRTB" if x in ms) + " → повернуть " + "/".join(core.MOVE_RU[x] for x in "LRTB" if x in ms))
        for i, (c, win) in enumerate(zip(L["cams"], L["rects"])):   # v26: window without a face -> fill it with the centre of the camera
            if i in idx or c not in imgs: continue
            h, w = imgs[c].shape[:2]; lp = core.layer_params(core.center_crop(w, h, win[2], win[3]), w, h, win)
            u0, v0, u1, v1 = lp["crop"]; fx, fy = L["flips"].get(c, (False, False))
            if fx: u0, u1 = 1 - u1, 1 - u0
            if fy: v0, v1 = 1 - v1, 1 - v0
            Ln = L["layers"][i]; inp = L["t"]["key"]
            if send:
                vm.fn(f"SetLayer{Ln}Zoom", Input=inp, Value=f"{lp['zoom']:.5f}"); vm.fn(f"SetLayer{Ln}PanX", Input=inp, Value=f"{lp['panx']:.5f}")
                vm.fn(f"SetLayer{Ln}PanY", Input=inp, Value=f"{lp['pany']:.5f}"); vm.fn(f"SetLayer{Ln}Crop", Input=inp, Value=f"{u0:.5f},{v0:.5f},{u1:.5f},{v1:.5f}")
        if not send:
            s.say(f"«{L['t']['title']}»: камеры {L['cams']}, лица " + ", ".join(f"{c}: {len(faces[c])}" for c in imgs) + " — предпросмотр, нажмите «Кадрировать»" + (" · " + "; ".join(warn) if warn else ""))
            s.show(None, L, crops, items, miss); return
        # readback
        ok = True
        try:
            inp = next(i for i in vm.state().find("inputs") if i.get("key") == L["t"]["key"])
            got = {int(o.get("index")) + 1: o for o in inp.findall("overlay")}
            for Ln, z, u0 in sent:
                p = got[Ln].find("position"); ok &= p is not None and abs(float(p.get("zoomX", 0)) - z) < 1e-3
        except Exception as e: ok = False; s.log(f"проверка: {e}")
        empty = [c for c in L["cams"] if not faces[c]]
        msg = (f"готово ✓ окон {len(sent)}/{len(L['cams'])}, крупность {us:.2f}" if ok else "⚠ vMix не подтвердил значения")
        if empty: msg += " · лиц нет на камере " + ", ".join(map(str, sorted(set(empty))))
        if warn: msg += " · " + "; ".join(warn)
        if L["how"] != "по рамкам": msg += " · окна " + L["how"]
        s.say(msg)
        s.show(None, L, crops, items, miss)

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
