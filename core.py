"""vMix auto-framing core: detection, framing math, vMix API."""
import os, sys, re, time, json, threading, urllib.request, urllib.parse, xml.etree.ElementTree as ET
import numpy as np, cv2

W, H = 1920, 1080  # vMix canvas

def res(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

WINDOW_RECTS = {}  # from config "window_rects": {"2": [[x,y,w,h],...]} = guest windows of the vMix plans (v6)

def layout_rects(n):
    """Guest windows of the plans: config window_rects, else MetrFM_N sets (make_vset.py)."""
    if str(n) in WINDOW_RECTS: return [tuple(int(v) for v in r) for r in WINDOW_RECTS[str(n)]]
    X0, X1, Y0, Y1, g = 330, 1890, 170, 800, 30
    w = (X1 - X0 - g * (n - 1)) / n
    return [(round(X0 + i * (w + g)), Y0, round(w), Y1 - Y0) for i in range(n)]

# ---------- detection ----------
_dets = {}; _det_lock = threading.Lock()
def _detect_raw(img, score):
    h, w = img.shape[:2]
    for attempt in (0, 1):
        try:
            with _det_lock:  # one detector per input size; OpenCV 5 dnn is strict about shapes and threads
                d = _dets.get((w, h))
                if d is None or attempt:
                    d = cv2.FaceDetectorYN.create(res("face_detection_yunet_2023mar.onnx"), "", (w, h), score, 0.3, 50); _dets[(w, h)] = d
                d.setInputSize((w, h)); d.setScoreThreshold(score)
                return d.detect(img)[1]
        except cv2.error as e:
            _dets.pop((w, h), None)
            if attempt: raise
    return None

def detect_faces(img, score=0.6, log=None):
    """Returns list of (x,y,w,h,eye_y) in image pixels, sorted left->right. Never raises: 0 faces on error."""
    if img is None or img.size == 0: return []
    if img.ndim == 2: img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4: img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    img = np.ascontiguousarray(img, dtype=np.uint8)
    h, w = img.shape[:2]
    try: faces = _detect_raw(img, score)
    except Exception as e:
        (log or print)(f"детектор лиц: ошибка на кадре {w}x{h}, считаю 0 лиц ({str(e).splitlines()[0][:120]})"); return []
    out = []
    if faces is not None:
        for f in faces:
            x, y, fw, fh = f[:4]
            eye_y = (f[5] + f[7]) / 2  # right/left eye y
            if fw < w * 0.02: continue
            out.append((float(x), float(y), float(fw), float(fh), float(eye_y)))
    # merge duplicates (overlapping boxes)
    out.sort(key=lambda b: b[0]); merged = []
    for b in out:
        if merged and abs((b[0] + b[2] / 2) - (merged[-1][0] + merged[-1][2] / 2)) < max(b[2], merged[-1][2]) * 0.5:
            continue
        merged.append(b)
    return merged

# ---------- framing math ----------
def person_crop(face, img_w, img_h, win_w, win_h, size=3.4, eye_frac=0.42):
    """Crop rect (pixels) with window aspect: head+shoulders, eyes at ~1/3 from top."""
    x, y, fw, fh, ey = face
    # crop in image pixels must map to window aspect after the 16:9 stretch:
    # (cw/img_w*W)/(ch/img_h*H) = win_w/win_h  ->  cw = ch * (win_w/win_h) * (img_w/W) / (img_h/H)
    ch = min(img_h, fh * size)
    cw = ch * (win_w / win_h) * (img_w / W) / (img_h / H)
    if cw > img_w: cw = img_w; ch = cw / ((win_w / win_h) * (img_w / W) / (img_h / H))
    cx = x + fw / 2
    left = min(max(cx - cw / 2, 0), img_w - cw)
    top = min(max(ey - ch * eye_frac, 0), img_h - ch)
    return left, top, cw, ch

def uniform_crops(items, cfg=None, info=None):
    """items: [(face, img_w, img_h, win_w, win_h)]. One common size for all guests (size = crop_h/face_h,
    smaller = bigger face); face centred, eyes on eye_line. The fader value is always applied.
    fill_limit = largest size at which every camera still fills its window; above it the picture is
    shrunk inside the window (empty field). info (dict) gets fill_limit and windows with a field."""
    cfg = cfg or {}
    ef = cfg.get("eye_line", 0.42)
    s = min(max(float(cfg.get("frame_size", 3.6)), cfg.get("frame_size_min", 1.3)), cfg.get("frame_size_max", 8.0))
    lim = 99.0; per = []
    for (x, y, fw, fh, ey), iw, ih, ww, wh in items:
        k = (ww / wh) * (iw / W) / (ih / H); cx = x + fw / 2
        l = min(ey / (fh * ef), (ih - ey) / (fh * (1 - ef)), 2 * cx / (fh * k), 2 * (iw - cx) / (fh * k))
        per.append(l); lim = min(lim, l)
    if cfg.get("fill_frame") == "strict": s = max(min(s, lim), cfg.get("frame_size_min", 1.3))
    s = max(s, float(cfg.get("_s_min", 0)))   # «Уместить всех в поле»: never tighter than needed to fit everybody
    if info is not None:
        info["fill_limit"] = lim; info["field_windows"] = [i + 1 for i, l in enumerate(per) if s > l + 1e-6]
    out = []
    smin, smax = float(cfg.get("frame_size_min", 1.3)), float(cfg.get("frame_size_max", 8.0))
    s0 = max(min(float(cfg.get("frame_size", 3.6)), smax), smin)
    # v28: the slider spans the FEASIBLE range for these cameras/windows:
    #   left end  = tightest allowed (head+shoulders / «уместить всех»), right end = whole camera picture (no empty field).
    #   One common value for all windows -> equal head sizes. cfg no_field=False -> old behaviour (may show empty field).
    lo = max(float(cfg.get("_s_min", 0)), smin * max((W / H) / (it[3] / it[4]) for it in items) if items else smin)
    if cfg.get("no_field", True) and items:
        hi = min(min(ih / fh, iw / (fh * ((ww / wh) * (iw / W) / (ih / H)))) for (x, y, fw, fh, ey), iw, ih, ww, wh in items)
        hi = max(hi, lo); si = lo + (s0 - smin) / max(1e-6, smax - smin) * (hi - lo)
    else:
        si = max(s0, lo)
    if info is not None: info["eff_size"] = si
    for (x, y, fw, fh, ey), iw, ih, ww, wh in items:
        k = (ww / wh) * (iw / W) / (ih / H)
        ch = fh * si; cw = ch * k
        out.append((x + fw / 2 - cw / 2, ey - ch * ef, cw, ch))
    return out, s

def center_crop(img_w, img_h, win_w, win_h, cx_frac=0.5):
    ch = img_h; cw = ch * (win_w / win_h) * (img_w / W) / (img_h / H)
    if cw > img_w: cw = img_w; ch = cw / ((win_w / win_h) * (img_w / W) / (img_h / H))
    left = min(max(img_w * cx_frac - cw / 2, 0), img_w - cw)
    return left, (img_h - ch) / 2, cw, ch

def layer_params(crop, img_w, img_h, win):
    """vMix layer zoom/pan/crop so crop (image px) fills window win=(x,y,w,h) on canvas."""
    l, t, cw, ch = crop; wx, wy, ww, wh = win
    u0, v0, u1, v1 = l / img_w, t / img_h, (l + cw) / img_w, (t + ch) / img_h
    z = wh / (H * (v1 - v0))
    cxf = wx - u0 * W * z + W * z / 2
    cyf = wy - v0 * H * z + H * z / 2
    cl = lambda v: min(max(v, 0.0), 1.0)  # crop outside the picture = empty field, vMix crop only masks
    return dict(zoom=z, panx=(cxf - W / 2) / (W / 2), pany=(H / 2 - cyf) / (H / 2), crop=(cl(u0), cl(v0), cl(u1), cl(v1)))

def render(sources, assignments, rects, background=None, overlays=()):
    """Simulate the vMix composite (for testing/preview)."""
    canvas = np.zeros((H, W, 3), np.uint8) if background is None else cv2.resize(background, (W, H))
    for (si, crop), win in zip(assignments, rects):
        img = sources[si]; l, t, cw, ch = [int(round(v)) for v in crop]
        part = cv2.resize(img[t:t + ch, l:l + cw], (win[2], win[3]), interpolation=cv2.INTER_AREA)
        canvas[win[1]:win[1] + win[3], win[0]:win[0] + win[2]] = part
    for ov in overlays:
        if ov is None: continue
        a = ov[:, :, 3:4] / 255.0; canvas = (ov[:, :, :3] * a + canvas * (1 - a)).astype(np.uint8)
    return canvas

def plan(sources, n_max=4, rects_for=layout_rects):
    """sources: list of images. Returns (n, rects, assignments[(src_idx, crop)], faces_per_source)."""
    faces = [detect_faces(s) for s in sources]
    persons = [(si, f) for si, fl in enumerate(faces) for f in fl][:n_max]
    n = max(1, len(persons)); rects = rects_for(n); asg = []
    if not persons:
        h, w = sources[0].shape[:2]; asg = [(0, center_crop(w, h, rects[0][2], rects[0][3]))]
    for (si, f), win in zip(persons, rects):
        h, w = sources[si].shape[:2]
        asg.append((si, person_crop(f, w, h, win[2], win[3])))
    return n, rects, asg, faces

# ---------- vMix ----------
class VMix:
    def __init__(self, url="http://127.0.0.1:8088/api", log=print):
        self.url = url.rstrip("/"); self.log = log; self.offsets = {}; self.readonly = False; self.skipped = 0; self.remote = False; self.allow_snap = False
    def fn(self, function, **params):
        if self.readonly:   # v16: dry run / remote read-only - never send a Function
            self.skipped += 1; return ""
        q = {"Function": function}; q.update({k: str(v) for k, v in params.items()})
        u = self.url + "/?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)
        r = urllib.request.urlopen(u, timeout=5).read().decode("utf-8", "replace")
        time.sleep(0.05); return r
    def state(self):
        return ET.fromstring(urllib.request.urlopen(self.url, timeout=5).read())
    def inputs(self):
        return {i.get("number"): i for i in self.state().find("inputs")}
    def find(self, title):
        for i in self.state().find("inputs"):
            if i.get("title") == title: return i
    def snapshot(self, inp, path, timeout=15):
        if self.readonly or (self.remote and not self.allow_snap): raise RuntimeError("снимок через этот vMix запрещён (удалённый / только чтение)")
        if os.path.exists(path): os.remove(path)
        self.fn("SnapshotInput", Input=inp, Value=path)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                time.sleep(0.3)
                img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
                if img is not None: return img
            time.sleep(0.2)
        raise RuntimeError(f"нет снимка входа {inp}")
    def input_type(self, src):
        for i in self.state().find("inputs"):
            if i.get("number") == str(src) or i.get("key") == str(src) or i.get("title") == str(src): return i.get("type")
    def set_layer(self, layout, idx, src, p, flip_types=("VLC",)):
        """vMix quirk (checked on tdir): for VLC inputs the layer crop is mirrored on both axes."""
        u0, v0, u1, v1 = p["crop"]
        fl = getattr(self, "flips", {}).get(str(src))
        if fl is None: fl = (True, True) if self.input_type(src) in flip_types else (False, False)
        if fl[0]: u0, u1 = 1 - u1, 1 - u0
        if fl[1]: v0, v1 = 1 - v1, 1 - v0
        p = dict(p, crop=(u0, v0, u1, v1))
        lm = getattr(self, "layer_map", {}).get(layout)
        idx = lm[idx - 1] if lm else idx + self.offsets.get(layout, 0)  # window number -> vMix layer number
        if not self.remote: self.fn("SetLayer", Input=layout, Value=f"{idx},{src}")   # v17: remote = only zoom/pan/crop
        self.fn(f"SetLayer{idx}Zoom", Input=layout, Value=f"{p['zoom']:.5f}")
        self.fn(f"SetLayer{idx}PanX", Input=layout, Value=f"{p['panx']:.5f}")
        self.fn(f"SetLayer{idx}PanY", Input=layout, Value=f"{p['pany']:.5f}")
        self.fn(f"SetLayer{idx}Crop", Input=layout, Value=",".join(f"{c:.5f}" for c in p["crop"]))

def layer_offset(vm, inp, n, cfg):
    """How many vMix layers lie under the guest windows (city/background layer etc.).
    cfg layer_offset: "auto" (default) or a number. Auto: guests are the n layers right below
    the "<title> рамки" layer; without рамки a full-frame non-camera layer 0 is a background."""
    v = cfg.get("layer_offset", "auto")
    if str(v).lower() != "auto": return int(v)
    st = vm.state(); titles = {i.get("key"): i.get("title") or "" for i in st.find("inputs")}
    me = next((i for i in st.find("inputs") if i.get("key") == inp.get("key")), inp)
    ovs = sorted(((int(o.get("index")), o.get("key")) for o in me.findall("overlay")))
    for k, key in ovs:
        if titles.get(key, "").endswith(" рамки"): return max(k - n, 0)
    if ovs and ovs[0][0] == 0:
        src_keys = {i.get("key") for i in st.find("inputs") if i.get("number") in {str(x) for x in cfg.get("sources", [])}}
        if ovs[0][1] not in src_keys: return 1
    return 0

def ensure_layout(vm, n, cfg):
    """Find or build layout input for n guests (background + guests layers + borders + desk)."""
    title = cfg["layout_titles"].get(str(n), f"МЭТР FM {n} гостей")
    i = vm.find(title)
    if i is not None:
        vm.offsets[i.get("key")] = layer_offset(vm, i, n, cfg)
        if vm.offsets[i.get("key")]: vm.log(f"«{title}»: окна гостей со слоя {vm.offsets[i.get('key')] + 1} (ниже фон)")
        return i.get("key"), False
    d = os.path.join(cfg["sets_dir"], f"MetrFM_{n}guest" + ("" if n == 1 else "s"))
    keys = []
    for f, t in (("background.jpg", title), ("borders.png", f"{title} рамки"), ("desk.png", f"{title} стол")):
        before = set(vm.inputs().keys())
        vm.fn("AddInput", Value="Image|" + os.path.join(d, f)); time.sleep(1.0)
        new = [v for k, v in vm.inputs().items() if k not in before]
        if not new: raise RuntimeError("vMix не добавил " + f)
        vm.fn("SetInputName", Input=new[0].get("key"), Value=t); keys.append(new[0].get("key"))
    vm.fn("SetLayer", Input=keys[0], Value=f"{n + 1},{keys[1]}")
    vm.fn("SetLayer", Input=keys[0], Value=f"{n + 2},{keys[2]}")
    vm.offsets[keys[0]] = 0
    vm.log(f"создан вход «{title}»")
    return keys[0], True

def refine(vm, key, rects, applied, cfg, tmpdir, log=print):
    """Closed loop: snapshot the layout, measure each guest's face in its window and correct
    zoom/pan/crop so face height = win_h/frame_size, eyes on eye_line, face centred.
    applied: [(idx, src, params)] only for windows with a person. Returns measurements."""
    ef = cfg.get("eye_line", 0.42); s = float(cfg.get("_eff_size", cfg.get("frame_size", 3.6))); flip = tuple(cfg.get("flip_crop_types", ["VLC"]))
    time.sleep(float(cfg.get("refine_delay", 0.6)))
    img = vm.snapshot(key, os.path.join(tmpdir, "refine.png"))
    meas = []
    for idx, src, p in applied:
        wx, wy, ww, wh = rects[idx - 1]
        fs = detect_faces(img[wy:wy + wh, wx:wx + ww], cfg.get("refine_score", 0.4))
        if len(fs) != 1: meas.append((idx, None)); continue
        x, y, fw, fh, ey = fs[0]
        if not (0.4 < fh / (wh / s) < 2.5): meas.append((idx, None)); continue   # v14: implausible face -> do not chase it
        meas.append((idx, (fh, ey, x + fw / 2)))
        z = p["zoom"]; Cx = W / 2 + p["panx"] * W / 2; Cy = H / 2 - p["pany"] * H / 2
        Px, Py = wx + x + fw / 2, wy + ey
        r = min(max((wh / s) / fh, 0.6), 1.6)
        if abs(r - 1) < 0.03 and abs(Px - (wx + ww / 2)) < ww * 0.02 and abs(Py - (wy + ef * wh)) < wh * 0.02: continue
        z2 = z * r; Tx, Ty = wx + ww / 2, wy + ef * wh
        C2x = Tx - (Px - Cx) * r; C2y = Ty - (Py - Cy) * r
        u0 = (wx - C2x) / (z2 * W) + 0.5; u1 = (wx + ww - C2x) / (z2 * W) + 0.5
        v0 = (wy - C2y) / (z2 * H) + 0.5; v1 = (wy + wh - C2y) / (z2 * H) + 0.5
        cl = lambda v: min(max(v, 0.0), 1.0)
        q = dict(zoom=z2, panx=(C2x - W / 2) / (W / 2), pany=(H / 2 - C2y) / (H / 2), crop=(cl(u0), cl(v0), cl(u1), cl(v1)))
        vm.set_layer(key, idx, src, q, flip)
        log(f"коррекция окна {idx}: лицо {fh:.0f}→{wh / s:.0f}px, глаза {ey:.0f}→{ef * wh:.0f}px, центр {x + fw / 2:.0f}→{ww / 2:.0f}px")
    return meas

def measure(vm, key, rects, tmpdir):
    img = vm.snapshot(key, os.path.join(tmpdir, "measure.png")); out = []
    for i, (wx, wy, ww, wh) in enumerate(rects, 1):
        fs = detect_faces(img[wy:wy + wh, wx:wx + ww], 0.5)
        out.append((i, [(round(f[3]), round(f[4]), round(f[0] + f[2] / 2)) for f in fs]))
    return img, out

# ---------- auto colour (vMix colour correction: SetCCGain*/SetCCLift*/SetCCGamma*/SetCCSaturation) ----------
CC_KEYS = ["liftR", "liftG", "liftB", "liftY", "gammaR", "gammaG", "gammaB", "gammaY", "gainR", "gainG", "gainB", "gainY", "saturation", "hue"]
CC_NEUTRAL = dict(liftR=0, liftG=0, liftB=0, liftY=0, gammaR=0, gammaG=0, gammaB=0, gammaY=0, gainR=1, gainG=1, gainB=1, gainY=1, saturation=0, hue=0)
CC_FN = {k: "SetCC" + k[0].upper() + k[1:] for k in CC_KEYS}  # liftR -> SetCCLiftR ...

def read_cc(vm, src):
    for i in vm.state().find("inputs"):
        if i.get("number") == str(src) or i.get("key") == str(src) or i.get("title") == str(src):
            cc = dict(CC_NEUTRAL); e = i.find("cc")
            if e is not None:
                for k in CC_KEYS:
                    if e.get(k) is not None: cc[k] = float(e.get(k).replace(",", "."))
            return cc
    raise RuntimeError(f"нет входа {src}")

def write_cc(vm, src, cc):
    """Applies every CC value it can; returns the list of keys vMix refused (HTTP 500)."""
    failed = []
    for k in CC_KEYS:
        if k in cc:
            try: vm.fn(CC_FN[k], Input=src, Value=f"{cc[k]:.4f}")
            except Exception: failed.append(k)
    return failed

def color_stats(img, faces):
    """Measures the picture: grey-world white balance on low-saturation midtones, face/frame luma."""
    f = img.astype(np.float32) / 255.0
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m = (hsv[..., 1] < 70) & (hsv[..., 2] > 40) & (hsv[..., 2] < 235)
    if m.sum() < img.shape[0] * img.shape[1] * 0.02: m = (hsv[..., 2] > 30) & (hsv[..., 2] < 240)
    b, g, r = [float(f[..., c][m].mean()) for c in range(3)]
    y = 0.299 * f[..., 2] + 0.587 * f[..., 1] + 0.114 * f[..., 0]
    fy = None
    if faces:
        x, yy, w, h, _ = max(faces, key=lambda q: q[3])
        x0, y0 = int(max(x + w * 0.2, 0)), int(max(yy + h * 0.25, 0)); x1, y1 = int(x + w * 0.8), int(yy + h * 0.85)
        if x1 > x0 and y1 > y0: fy = float(y[y0:y1, x0:x1].mean())
    return dict(r=r, g=g, b=b, frame_y=float(np.median(y)), face_y=fy, sat=float(hsv[..., 1].mean() / 255))

def auto_cc(cur, st, cfg):
    """New CC from current CC + measured stats (the snapshot already contains the current CC)."""
    cc = dict(cur); gm = (st["r"] + st["g"] + st["b"]) / 3
    lim = cfg.get("cc_wb_limit", 0.25)
    for c in "RGB":
        k = min(max(gm / max(st[c.lower()], 1e-3), 1 - lim), 1 + lim)
        cc["gain" + c] = min(max(cur["gain" + c] * k, 0.5), 1.8)
    tgt = cfg.get("cc_face_target", 0.56) if st["face_y"] is not None else cfg.get("cc_frame_target", 0.45)
    meas = st["face_y"] if st["face_y"] is not None else st["frame_y"]
    k = min(max(tgt / max(meas, 1e-3), 0.7), 1.5)
    cc["gainY"] = min(max(cur["gainY"] * k, 0.5), 1.8)
    cc["liftY"] = cfg.get("cc_lift", -0.03)          # deeper blacks = a bit more contrast
    cc["gammaY"] = cfg.get("cc_gamma", 0.0)
    cc["saturation"] = cfg.get("cc_saturation", 0.15)  # 0 = original
    return cc


# ---------- v9: templates discovered from any open preset ----------
CAM_TYPES = ("Stream", "VLC", "Capture", "NDI", "Virtual", "SRT", "VideoCall", "Replay")
NOT_CAM = re.compile(r"город|city|фон|background|подложк|шаблон|рамк|стол", re.I)

def _vis_rect(o, typ, flip_types, fl=None):
    pos = o.find("position")
    if pos is None: return None
    x, y, w, h = (float(pos.get(k, 0)) for k in ("x", "y", "width", "height"))
    c = o.find("crop"); X1, Y1, X2, Y2 = (0.0, 0.0, 1.0, 1.0) if c is None else (float(c.get(k)) for k in ("X1", "Y1", "X2", "Y2"))
    if fl is None: fl = (True, True) if typ in flip_types else (False, False)
    if fl[0]: X1, X2 = 1 - X2, 1 - X1   # same vMix quirk as in set_layer (v14: per input rotation)
    if fl[1]: Y1, Y2 = 1 - Y2, 1 - Y1
    x0, y0, x1, y1 = max(x + X1 * w, 0), max(y + Y1 * h, 0), min(x + X2 * w, W), min(y + Y2 * h, H)
    if x1 - x0 < 40 or y1 - y0 < 40: return None
    return (round(x0), round(y0), round(x1 - x0), round(y1 - y0))

def discover(state, cfg):
    """Every input whose layers hold camera inputs is a template; camera layers are its windows.
    Background (full frame / город), рамки, стол, logos are left alone. Returns list sorted by preference."""
    flip = tuple(cfg.get("flip_crop_types", ["VLC"])); names = cfg.get("template_names", [])
    inputs = list(state.find("inputs")); by_key = {i.get("key"): i for i in inputs}; out = []
    try: rot = preset_rotations(state)
    except Exception: rot = {}
    for inp in inputs:
        wins = []
        for o in inp.findall("overlay"):
            src = by_key.get(o.get("key"))
            if src is None or src.get("type") not in CAM_TYPES or NOT_CAM.search(src.get("title") or ""): continue
            r = _vis_rect(o, src.get("type"), flip, rot.get(src.get("key")))
            if r is None: r = _vis_rect(o, src.get("type"), flip, (False, False)) or (0, 0, 64, 64)   # v14: a camera layer is never lost
            if (r[2] > W * 0.95 and r[3] > H * 0.95): continue   # full-frame layer = background, not a window
            wins.append((int(o.get("index")) + 1, r, int(src.get("number"))))
        if not wins: continue
        wins.sort(key=lambda t: (t[1][0], t[1][1]))
        t = inp.get("title") or ""
        pri = next((k for k, nm in enumerate(names) if nm.lower() == t.lower()), len(names))
        out.append(dict(key=inp.get("key"), title=t, number=int(inp.get("number")), n=len(wins),
                        layers=[w[0] for w in wins], rects=[w[1] for w in wins], cams=[w[2] for w in wins], pri=pri))
    out.sort(key=lambda d: (d["pri"], d["n"], d["number"]))
    return out

def pick_template(tpls, n):
    exact = [t for t in tpls if t["n"] == n]
    if exact: return exact[0]
    bigger = sorted([t for t in tpls if t["n"] > n], key=lambda t: (t["n"], t["pri"]))
    if bigger: return bigger[0]
    return max(tpls, key=lambda t: (t["n"], -t["pri"])) if tpls else None

def auto_cameras(tpls):
    cams = sorted({c for t in tpls for c in t["cams"]})
    return cams


# ---------- v12: camera rotation, frame geometry, calibration ----------
def preset_rotations(state):
    """{input key: (flip_x, flip_y)} from the open preset file (SourceRotation/Mirror of the input itself).
    R180 = crop/pan of a layer showing this input is mirrored on both axes; Mirror = x only."""
    out = {}; path = state.findtext("preset") or ""
    try:
        if not path.lower().endswith(".vmix") or not os.path.exists(path): return out
        root = ET.parse(path).getroot()
    except Exception: return out
    for e in root.findall("Input"):
        pe = e.get("PositionsExtended") or e.get("Positions") or ""
        m = re.search(r"<SourceRotation>(R\d+)</SourceRotation>", pe); rot = m.group(1) if m else "R0"
        mir = (e.get("Mirror") or "").lower() == "true" or "<Mirror>true</Mirror>" in pe[:2000]
        fx = (rot == "R180") ^ mir; fy = rot == "R180"
        out[e.get("Key")] = (fx, fy)
    return out

def frame_windows(img_bgra, min_area=20000):
    """Windows = transparent holes enclosed by the opaque frame drawing (not touching the image border)."""
    if img_bgra is None or img_bgra.ndim != 3 or img_bgra.shape[2] != 4: return []
    a = img_bgra[:, :, 3]
    if a.max() == 0 or a.min() > 0: return []
    hole = (a < 40).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(hole, 4)
    h, w = a.shape; out = []
    for i in range(1, n):
        x, y, ww, hh, ar = st[i]
        if ar < min_area or x == 0 or y == 0 or x + ww >= w or y + hh >= h: continue
        sx, sy = W / w, H / h
        out.append((round(x * sx), round(y * sy), round(ww * sx), round(hh * sy)))
    return sorted(out)

def snapshot_rgba(vm, inp, path, timeout=15):
    if getattr(vm, "readonly", False) or (getattr(vm, "remote", False) and not getattr(vm, "allow_snap", False)): return None
    if os.path.exists(path): os.remove(path)
    vm.fn("SnapshotInput", Input=inp, Value=path); t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            time.sleep(0.3); img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_UNCHANGED)
            if img is not None: return img
        time.sleep(0.2)

def check_axes(vm, key, rects, applied, cfg, tmpdir, log=print):
    """Calibration by observation: a face placed at the window centre must appear there. If it lands far off
    on an axis, the crop direction for that camera/axis is the other one: try it and keep what fits better."""
    ef = cfg.get("eye_line", 0.42); done = vm.__dict__.setdefault("axes_checked", set())
    known = getattr(vm, "preset_flips", {}) if str(cfg.get("calibrate", "auto")) == "auto" else {}
    todo = [(i, s, p) for i, s, p in applied if str(s) not in done and str(s) not in known]   # v14: rotation from preset is trusted
    if not todo: return
    def meas():
        time.sleep(float(cfg.get("refine_delay", 0.6))); img = vm.snapshot(key, os.path.join(tmpdir, "axes.png")); out = {}
        for idx, src, p in todo:
            wx, wy, ww, wh = rects[idx - 1]; fs = detect_faces(img[wy:wy + wh, wx:wx + ww], 0.4)
            if len(fs) == 1: x, y, fw, fh, ey = fs[0]; out[idx] = (abs(x + fw / 2 - ww / 2) / ww, abs(ey - ef * wh) / wh)
        return out
    base = meas()
    for axis in (0, 1):
        for idx, src, p in todo:
            if idx not in base or base[idx][axis] < 0.12: continue
            fl = list(vm.flips.get(str(src), (False, False))); fl[axis] = not fl[axis]; old = vm.flips.get(str(src), (False, False))
            vm.flips[str(src)] = tuple(fl); vm.set_layer(key, idx, src, p)
            m = meas().get(idx)
            if m and m[axis] < base[idx][axis]:
                log(f"калибровка: вход {src}, ось {'XY'[axis]} — направление обрезки обратное, исправлено"); base[idx] = m
            else:
                vm.flips[str(src)] = old; vm.set_layer(key, idx, src, p)
    for idx, src, p in todo:
        if idx in base: done.add(str(src))


def group_people(persons, n):
    """persons [(cam, face)] -> at most n groups [(cam, [faces])]: neighbours on the same camera are merged
    (smallest gap first) so nobody is dropped. Returns groups and whether everybody could be kept."""
    groups = [(k, [f]) for k, f in persons]
    while len(groups) > n:
        best = None
        for i, (k, fs) in enumerate(groups):
            for j, (k2, fs2) in enumerate(groups):
                if j <= i or k2 != k: continue
                a0, a1 = min(f[0] for f in fs), max(f[0] + f[2] for f in fs)
                b0, b1 = min(f[0] for f in fs2), max(f[0] + f[2] for f in fs2)
                gap = max(b0 - a1, a0 - b1)
                if best is None or gap < best[0]: best = (gap, i, j)
        if best is None: break
        _, i, j = best; groups[i] = (groups[i][0], groups[i][1] + groups[j][1]); del groups[j]
    return groups, len(groups) <= n

def group_face(fs):
    """One pseudo-face for a group: box over all faces, average head height and eye line."""
    x0 = min(f[0] for f in fs); x1 = max(f[0] + f[2] for f in fs)
    y0 = min(f[1] for f in fs); fh = sum(f[3] for f in fs) / len(fs); ey = sum(f[4] for f in fs) / len(fs)
    return (x0, y0, x1 - x0, fh, ey)

def fit_size(gface, fw_one, img_w, img_h, win_w, win_h, ef=0.42):
    """Smallest frame_size (crop_h / face_h) at which the group plus shoulders fits the window."""
    x, y, gw, fh, ey = gface; k = (win_w / win_h) * (img_w / W) / (img_h / H)
    need_w = gw + 2.0 * fw_one          # a face width of shoulder room on each side
    return max(need_w / (fh * k), 2.4)  # and at least head + shoulders vertically

def whole_size(gface, img_w, img_h, win_w, win_h):
    """frame_size at which the crop is the whole camera picture (zooming out further only shrinks the picture)."""
    x, y, gw, fh, ey = gface; k = (win_w / win_h) * (img_w / W) / (img_h / H)
    return min(img_h / fh, img_w / (fh * k))


# ---------- v15: where the camera picture is missing ----------
SIDE_RU = {"L": "слева", "R": "справа", "T": "сверху", "B": "снизу"}
MOVE_RU = {"L": "влево", "R": "вправо", "T": "вверх", "B": "вниз"}
def missing_sides(crop, img_w, img_h, tol=0.02):
    """crop (image px) vs picture: {side: missing fraction of crop} for sides where the crop leaves the picture."""
    l, t, cw, ch = crop; out = {}
    if l < -tol * cw: out["L"] = -l / cw
    if l + cw > img_w + tol * cw: out["R"] = (l + cw - img_w) / cw
    if t < -tol * ch: out["T"] = -t / ch
    if t + ch > img_h + tol * ch: out["B"] = (t + ch - img_h) / ch
    return out


# ---------- v16: remote vMix address ----------
def api_url(host):
    h = (host or "127.0.0.1:8088").strip()
    if h.startswith("http"): return h.rstrip("/") if h.rstrip("/").endswith("/api") else h.rstrip("/") + "/api"
    if ":" not in h: h += ":8088"
    return f"http://{h}/api"

def is_local(host):
    h = (host or "").replace("http://", "").split("/")[0].split(":")[0].lower()
    return h in ("", "127.0.0.1", "localhost")

def parse_map(s):
    """'5=2,7=3' -> {5: 2, 7: 3}: target-vMix camera input -> input on the picture vMix."""
    out = {}
    for part in (s or "").replace(" ", "").split(","):
        if "=" in part:
            a, b = part.split("=", 1)
            if a.isdigit() and b.isdigit(): out[int(a)] = int(b)
    return out


# ---------- v18: DP Thumbnail Server (adelvo directors-plan) as a read-only picture source ----------
def dp_thumb(base, key, timeout=10):
    """GET <base>/key/<input key>.jpg - plain image fetch, no vMix Function, no regen."""
    u = base.rstrip("/") + "/key/" + key + ".jpg"
    data = urllib.request.urlopen(u, timeout=timeout).read()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None: raise RuntimeError("DP thumbnail: не картинка")
    return img


def dp_frame_windows(base, state, inp_key, title_re):
    """v20: window holes of all frame-image layers of a plan, from DP thumbnails (white frame on dark), in canvas coords,
    honouring each frame layer's own position (x, y, width, height)."""
    by_key = {i.get("key"): i for i in state.find("inputs")}
    plan = by_key[inp_key]; out = []; cache = {}
    for o in plan.findall("overlay"):
        src = by_key.get(o.get("key"))
        if src is None or src.get("type") not in ("Image", "ImageSequence") or not re.search(title_re, src.get("title") or "", re.I): continue
        if o.get("key") not in cache:
            img = dp_thumb(base, o.get("key")); g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA); bgra[:, :, 3] = np.where(g > 200, 255, 0).astype(np.uint8)
            cache[o.get("key")] = frame_windows(bgra)
        pos = o.find("position"); fx, fy, fw, fh = 0.0, 0.0, float(W), float(H)
        if pos is not None and pos.get("width"):
            fx, fy, fw, fh = (float(pos.get(k)) for k in ("x", "y", "width", "height"))
        for (x, y, w, h) in cache[o.get("key")]:
            sx, sy = fw / W, fh / H
            out.append((round(fx + x * sx), round(fy + y * sy), round(w * sx), round(h * sy)))
    return sorted(set(out))


# ---------- v26: windows from the alpha channel of a video decoration layer (.mov with alpha) ----------
_MEDIA_CACHE = {}
def find_media(title, dirs):
    name = (title or "").strip()
    if not name: return None
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(p): return p
    for d in dirs:
        if (d, name) in _MEDIA_CACHE: return _MEDIA_CACHE[(d, name)]
        found = None
        for root, _, files in os.walk(d):
            if name in files: found = os.path.join(root, name); break
        _MEDIA_CACHE[(d, name)] = found
        if found: return found
    return None

def video_alpha_windows(state, plan_key, dirs, ffmpeg="ffmpeg", tmpdir=None, log=print, cache_path=None):
    """v29: windows = transparent holes of the decoration video. Order: cache file (окна.json) -> ffmpeg on the .mov."""
    import subprocess, shutil, tempfile
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try: cache = json.load(open(cache_path, encoding="utf-8-sig"))
        except Exception as e: log(f"окна.json не прочитан: {e}")
    ff = None
    for cand in [os.path.join(d, "ffmpeg.exe") for d in dirs] + [ffmpeg]:
        ff = shutil.which(cand) or (cand if os.path.isfile(cand) else None)
        if ff: break
    by_key = {i.get("key"): i for i in state.find("inputs")}; plan = by_key[plan_key]; out = []
    for o in plan.findall("overlay"):
        src = by_key.get(o.get("key"))
        if src is None or src.get("type") not in ("Video", "AudioFile", "VideoList"): continue
        wins = None; title = src.get("title") or ""
        if title in cache: wins = [tuple(w) for w in cache[title]]; log(f"окна «{title}» из окна.json: {wins}")
        if wins is None:
            if not ff: log("ffmpeg не найден и окна.json не содержит это видео"); continue
            path = find_media(title, dirs)
            if not path: log(f"видео-оформление «{title}»: файл не найден в {dirs}"); continue
            png = os.path.join(tmpdir or tempfile.gettempdir(), f"alpha_{o.get('key')[:8]}.png")
            try:
                subprocess.run([ff, "-v", "error", "-y", "-ss", "1", "-i", path, "-frames:v", "1", "-pix_fmt", "rgba", png],
                               check=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                img = cv2.imdecode(np.fromfile(png, np.uint8), cv2.IMREAD_UNCHANGED)
            except Exception as e: log(f"ffmpeg: {e}"); continue
            wins = frame_windows(img)
            if not wins: continue
            if cache_path:
                cache[title] = [list(w) for w in wins]
                try: json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                except OSError: pass
        pos = o.find("position"); fx, fy, fw, fh = 0.0, 0.0, float(W), float(H)
        if pos is not None and pos.get("width"): fx, fy, fw, fh = (float(pos.get(k)) for k in ("x", "y", "width", "height"))
        for (x, y, w, h) in wins:
            out.append((round(fx + x * fw / W), round(fy + y * fh / H), round(w * fw / W), round(h * fh / H)))
        log(f"окна из альфа-канала «{src.get('title')}»: {wins}")
    return sorted(set(out))
