#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
p0_app.py — 검사기 화면 (Tkinter)

  APP_UI.md 의 화면 셋(시작 → 진행 → 완료)을 그립니다.
  검사 로직은 여기 없습니다. 엔진(p0_survey.py)을 부르고,
  진행 상황을 받아 화면에 표시할 뿐입니다.

  ── 엔진은 건드리지 않습니다 ──────────────────────────────
    엔진에는 이미 진행 상황을 넘겨 주는 통로가 있습니다.
      · PROGRESS_HOOK(kind, **info)   단계 시작·진행·완료
      · ReadCache                     한 번 읽은 것은 저장
      · run_with_estimate             예상 시간을 재서 진행
    화면은 이 통로에 함수만 꽂습니다 (APP_UI 7항).

  ── 반드시 지키는 것 (APP_UI 8항) ────────────────────────
    · --share 로만 실행 → 결과에 파일 이름이 안 들어감
    · --selftest 가 통과해야 시작 → 옛 파일이면 잘못된 결과
    · 읽기 전용 (엔진이 보장)
    · 못 읽은 것을 숨기지 않음 → 완료 화면에 표시

  ── 화면이 얼어붙지 않게 (APP_UI 7항) ────────────────────
    검사는 별도 스레드에서 돌리고, 화면은 주 스레드에서만 갱신합니다.
    스레드끼리는 큐(queue)로만 이야기합니다. Tkinter 는 주 스레드에서만
    안전하기 때문입니다.
"""

import os
import time
import queue
import threading
import unicodedata

import tkinter as tk
from tkinter import ttk

# 엔진. 같은 폴더의 p0_survey.py 를 그대로 씁니다 (수정하지 않음).
import p0_survey as engine


# ─────────────────────────────────────────────────────────────
#  색 (APP_UI 7항)
#
#  검사기는 시리즈의 제품이 아니라 조사 도구입니다. 그래서 강조색을
#  무채색에 가깝게 둡니다 — 눈에 띄는 브랜드색이 필요 없습니다.
# ─────────────────────────────────────────────────────────────

BG      = "#F5F5F7"     # 바탕
CARD    = "#FFFFFF"     # 카드(안심 문구·찾은 것) 배경
INK     = "#1D1D1F"     # 본문 글자
SUB     = "#6E6E73"     # 보조 글자
LINE    = "#D2D2D7"     # 옅은 경계선
ACCENT  = "#3A3A3C"     # 강조 (거의 무채색)
OK      = "#1F7A5C"     # 완료 체크
WARN    = "#8A6D3B"     # 경고 문구 (위험색 아님)

WIN_W, WIN_H = 560, 640        # 고정 크기 (설명 문구가 들어가 세로를 늘림)

FONT     = ("AppleSDGothicNeo", 13)
FONT_SM  = ("AppleSDGothicNeo", 11)
FONT_BIG = ("AppleSDGothicNeo", 22)
FONT_H   = ("AppleSDGothicNeo", 16)
FONT_NUM = ("AppleSDGothicNeo", 13)


# ─────────────────────────────────────────────────────────────
#  엔진 단계 이름 → 사람 말 (APP_UI 3항)
#
#  프로그램 내부 이름을 그대로 화면에 쓰지 않습니다.
# ─────────────────────────────────────────────────────────────

STEP_HUMAN = {
    "환경 확인":            "이 맥을 살펴보는 중…",
    "파일 훑기":            "파일을 세는 중…",
    "캐시·렌더 확인":       "다시 생기는 파일을 확인하는 중…",
    "버전 가족 묶기":       "비슷한 파일을 묶는 중…",
    "완전 중복 검사":       "똑같은 파일을 찾는 중…",
    "자동 백업 찾기":       "자동 백업을 찾는 중…",
    "3D 파일 세기":         "3D 파일을 세는 중…",
    "참조·한글·비어도비 세기": "파일 종류를 살펴보는 중…",
    ".blend 머리말":        "3D 미리보기를 확인하는 중…",
    "낱말 빈도":            "이름을 살펴보는 중…",
    "폴더 모양":            "폴더 성격을 살펴보는 중…",
    "계보 (XMP)":           "파일 안의 작업 이력을 읽는 중…",
    "PSD 헤더":             "포토샵 파일을 살펴보는 중…",
    "AI 생성 흔적":         "AI로 만든 흔적을 찾는 중…",
    "썸네일 (qlmanage)":    "미리보기를 확인하는 중…",
    "겉모습 지문 (sips)":   "비슷하게 생긴 것을 찾는 중…",
}


def support_dir():
    """캐시·단계기록을 둘 곳. 앱 번들 안은 못 쓰므로 지원 폴더에 둡니다."""
    d = os.path.expanduser("~/Library/Application Support/맥파일검사")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = engine.tempfile.gettempdir()
    return d


def desktop_report_path():
    """결과 파일 경로. 바탕화면에 시각을 붙여 만듭니다 (APP_UI 4항)."""
    stamp = time.strftime("%m%d_%H%M")
    desktop = os.path.expanduser("~/Desktop")
    if not os.path.isdir(desktop):
        desktop = os.path.expanduser("~")
    return os.path.join(desktop, f"맥검사_{stamp}.txt")


# ─────────────────────────────────────────────────────────────
#  검사 일꾼 — 별도 스레드에서 엔진을 순서대로 돌립니다
#
#  main() 을 통째로 부르지 않고 여기서 단계를 늘어놓는 이유:
#    · 멈췄을 때도 '여기까지의 결과'를 보고서로 남겨야 하는데
#      (APP_UI 5항), main() 은 끝에서 한 번에 쓰기 때문에
#      중간 상태를 꺼낼 수 없습니다.
#    · 오래 걸리는 단계 앞에서 한 번 더 물어봐야 하는데 (APP_UI 3항),
#      그 분기점이 필요합니다.
#  엔진 함수는 그대로 부릅니다 — 고치는 게 아니라 쓰는 것입니다.
# ─────────────────────────────────────────────────────────────

class SurveyWorker(threading.Thread):

    def __init__(self, ui_queue, stop_flag, heavy_gate):
        super().__init__(daemon=True)
        self.q = ui_queue
        self.stop_flag = stop_flag        # threading.Event — 멈추기
        self.heavy_gate = heavy_gate      # HeavyGate — 오래 걸리는 단계 동의
        self.report_path = desktop_report_path()
        # 멈췄을 때도 '여기까지'를 보고서로 남기려면, 중간 상태를 인스턴스에
        # 들고 있어야 합니다. 지역변수로 두면 KeyboardInterrupt 때 사라집니다.
        self.sv = None
        self.a = {}

    # ── 화면으로 보내기 ─────────────────────────────────────
    def emit(self, kind, **info):
        self.q.put((kind, info))

    def _check_stop(self):
        """단계 사이에서 멈춤을 확인합니다. 눌렀으면 되감아 빠져나갑니다."""
        if self.stop_flag.is_set():
            raise KeyboardInterrupt

    # ── 엔진이 부르는 콜백 ──────────────────────────────────
    def _hook(self, kind, **info):
        """엔진의 PROGRESS_HOOK. 엔진 스레드(=이 스레드)에서 불립니다.

        멈춤이 눌렸으면 KeyboardInterrupt 를 올립니다.
        엔진의 _notify 는 Exception 만 삼키고 KeyboardInterrupt 는
        통과시키므로(BaseException), 이걸로 검사를 안전하게 되감습니다.
        """
        if self.stop_flag.is_set():
            raise KeyboardInterrupt
        if kind == "step":
            human = STEP_HUMAN.get(info.get("name"), info.get("name", ""))
            self.emit("step", label=human)
        elif kind == "tick":
            self.emit("tick", pct=info.get("pct", 0),
                      elapsed=info.get("elapsed", 0), left=info.get("left", 0))
        # step_done 는 화면에 따로 안 씁니다 (찾은 것으로 대신 보여줌).

    def run(self):
        engine.PROGRESS_HOOK = self._hook
        t0 = time.time()
        stopped = False
        try:
            self._pipeline()
        except KeyboardInterrupt:
            # 멈췄어도 self.sv·self.a 에 여기까지가 담겨 있습니다.
            stopped = True
        except Exception as e:
            # 실패를 숨기지 않습니다 (DECISIONS 7항).
            engine.PROGRESS_HOOK = None
            self.emit("error", message=f"{type(e).__name__}: {e}")
            return
        finally:
            engine.PROGRESS_HOOK = None

        # 끝났든 멈췄든, 여기까지의 결과를 보고서로 남깁니다 (APP_UI 5항).
        try:
            self._finalize(self.sv, self.a, time.time() - t0)
        except Exception as e:
            self.emit("error", message=f"보고서를 쓰지 못했습니다: {e}")
            return

        kind = "stopped" if stopped else "done"
        self.emit(kind, report=self.report_path,
                  **self._headline(self.sv, self.a))

    # ── 실제 순서 (main() 을 화면용으로 옮겨 놓은 것) ────────
    def _pipeline(self):
        cache = engine.ReadCache(os.path.join(support_dir(), "p0_cache.json"))
        log = engine.StepLog(os.path.join(support_dir(), "p0_steps.log"))
        a = self.a = {"cache": cache, "steplog": log}

        a["env"] = log.run("환경 확인", lambda: engine.environment())
        self._check_stop()

        sv = self.sv = engine.Survey("~")
        log.run("파일 훑기", lambda: (sv.walk(), sv.files)[1])
        self._found(sv, a)                      # 파일 수·용량이 처음 채워짐
        self._check_stop()

        log.run("캐시·렌더 확인", lambda: sv.scan_known_caches())
        self._found(sv, a)                      # '다시 생기는 것'
        self._check_stop()

        a["families"] = log.run("버전 가족 묶기",
                                lambda: engine.cluster_versions(sv.files))
        self._found(sv, a)                      # '버전 가족'
        self._check_stop()

        dup_cands = [f for f in sv.files if f[1] >= (1 << 20)]
        dups = engine.run_with_estimate(
            log, "완전 중복 검사", dup_cands,
            lambda pr: engine.find_duplicates(sv.files, progress=pr, cache=cache),
            auto_yes=True)                       # 앱에서는 절대 input() 안 함
        a["dups"] = dups if dups is not None else engine.find_duplicates([], quick=True)
        self._found(sv, a)                       # '똑같은 파일'
        self._check_stop()

        a["backups"] = log.run("자동 백업 찾기", lambda: engine.probe_backups(sv.files))
        a["threed"] = log.run("3D 파일 세기", lambda: engine.probe_3d(sv.files))
        a["counts"] = log.run("참조·한글·비어도비 세기",
                              lambda: engine.probe_counts(sv.files))
        a["blend"] = log.run(".blend 머리말", lambda: engine.probe_blend(sv.files))
        a["vocab"] = log.run("낱말 빈도", lambda: engine.build_token_vocab(sv.files))
        a["profiles"] = log.run("폴더 모양",
                                lambda: engine.profile_folders(sv.files, sv.btimes))
        a["unusual"] = engine.find_unusual_folders(a["profiles"])
        self._check_stop()

        # ── 여기부터 오래 걸립니다 (APP_UI 3항) ──────────────
        #   계보·썸네일은 파일 안을 하나씩 읽어 몇십 분이 걸릴 수 있습니다.
        #   그 앞에서 한 번 더 알리고, '여기까지만'을 정식 선택지로 줍니다.
        est = self._estimate_heavy(sv.files)
        go = self.heavy_gate.ask(self, est)
        # 창을 여는 사이에 '멈추기' 를 눌렀다면 그건 멈춤입니다(멈춤 화면으로).
        self._check_stop()
        if not go:
            # '여기까지만' — 1단계 결과만으로도 쓸 만합니다(완료 화면으로).
            return self._fill_none(a), sv

        xmp_cands = sorted([f for f in sv.files if f[3] in engine.XMP_PROBE_EXTS],
                           key=lambda f: -f[1])[:6000]
        lineage = engine.run_with_estimate(
            log, "계보 (XMP)", xmp_cands,
            lambda pr: engine.probe_lineage(sv.files, progress=pr, cache=cache),
            auto_yes=True, note="큰 것부터 — 회수량 계산에 필요")
        a["lineage"] = lineage
        if lineage:
            a["family_cmp"] = engine.compare_family_methods(
                a["families"], lineage["lineage_families"])
        self._check_stop()

        a["psd"] = log.run("PSD 헤더", lambda: engine.probe_psd(sv.files))
        a["ai"] = log.run("AI 생성 흔적", lambda: engine.probe_ai_metadata(sv.files))
        self._check_stop()

        # 외부 프로세스(qlmanage·sips) — 가장 느립니다.
        workdir = os.path.join(engine.tempfile.gettempdir(), "p0_probe")
        os.makedirs(workdir, exist_ok=True)
        try:
            a["thumbs"] = log.run("썸네일 (qlmanage)",
                                  lambda: engine.probe_thumbnails(sv.files, workdir),
                                  note="외부 프로세스 · 확장자별 표본")
            self._check_stop()
            a["phash"] = log.run("겉모습 지문 (sips)",
                                 lambda: engine.probe_phash_families(sv.files, workdir),
                                 note="외부 프로세스")
        finally:
            engine.shutil.rmtree(workdir, ignore_errors=True)

        return self._fill_none(a), sv

    @staticmethod
    def _fill_none(a):
        """건너뛴 단계는 None 으로 채웁니다. build_report 가 이를 견딥니다."""
        for k in ("lineage", "family_cmp", "psd", "ai", "thumbs", "phash",
                  "ownership"):
            a.setdefault(k, None)
        return a

    # ── 찾은 것 (진행 화면 패널) ────────────────────────────
    def _found(self, sv, a):
        recur = sum(v[1] for v in sv.cache_hits.values())
        dups = a.get("dups") or {}
        self.emit("found",
                  files=len(sv.files),
                  total_bytes=sv.total_bytes,
                  families=len(a.get("families") or []),
                  dup_bytes=dups.get("bytes", 0),
                  recur_bytes=recur)

    # ── 오래 걸리는 단계 예상 시간 ──────────────────────────
    def _estimate_heavy(self, files):
        """계보 단계가 대략 얼마나 걸릴지 표본으로 가늠합니다.

        엔진의 run_with_estimate 와 같은 발상이지만, 그건 값을 돌려주지
        않아서 여기서 가볍게 다시 잽니다(엔진 함수 read_xmp 를 그대로 사용).
        표본은 전체에서 고르게 뽑습니다 — 앞에서만 뽑으면 큰 것만 걸립니다.
        """
        targets = sorted([f for f in files if f[3] in engine.XMP_PROBE_EXTS],
                         key=lambda f: -f[1])[:6000]
        if not targets:
            return 0
        probe_n = min(24, len(targets))
        step = len(targets) / probe_n
        picked = [targets[int(i * step)] for i in range(probe_n)]
        t0 = time.time()
        read_bytes = 0
        for path, size, *_ in picked:
            try:
                engine.read_xmp(path, size)
                read_bytes += size
            except Exception:
                continue
        elapsed = time.time() - t0
        total_bytes = sum(f[1] for f in targets) or 1
        if read_bytes <= 0:
            return elapsed / max(probe_n, 1) * len(targets) * 1.5
        return elapsed * (total_bytes / read_bytes) * 1.5

    # ── 마무리: 보고서 쓰기 ─────────────────────────────────
    def _finalize(self, sv, a, elapsed):
        if sv is None:
            return
        cache = a.get("cache")
        # 아주 일찍 멈춰도 보고서가 나오게, 반드시 필요한 칸을 채웁니다.
        #   build_report 가 색인으로 읽는 것: age·names·families·dups·deriv
        a.setdefault("families", [])
        a["dups"] = a.get("dups") or engine.find_duplicates([], quick=True)
        # 완료 문장·등급 등 값싼 마무리 계산 (엔진 함수 그대로).
        try:
            a["grades"] = engine.grade_families(
                sv.files, a.get("dups"), a.get("families"),
                a.get("lineage"), (a.get("phash") or {}).get("families"))
        except Exception:
            a["grades"] = None
        a["validity"] = engine.measure_signal_validity(a.get("psd"), a.get("lineage"))
        try:
            a["advice_examples"] = engine.build_advice_examples(
                sv.files, a.get("lineage"), cache, a["validity"])
        except Exception:
            a["advice_examples"] = []
        a["phash_dist"] = engine.probe_phash_distance((a.get("phash") or {}).get("families"))
        a["dup_loc"] = engine.probe_dup_locations(a.get("dups"), sv.root)
        a["deriv"] = engine.find_derivatives(sv.files)
        a["names"] = engine.name_quality(sv.files)
        a["age"] = engine.age_buckets(sv.files)
        a.setdefault("ownership", None)

        if cache:
            cache.save()
        # ★ 공유 모드로만 씁니다 — 파일 이름이 결과에 안 들어갑니다.
        report = engine.build_report(a["env"], sv, a, elapsed, share=True)
        with open(self.report_path, "w", encoding="utf-8") as f:
            f.write(report)

    def _headline(self, sv, a):
        """완료·멈춤 화면에 크게 보여 줄 몇 개."""
        if sv is None:
            return {"files": 0, "total_bytes": 0, "reclaim": 0, "unread": 0}
        dups = a.get("dups") or {}
        backups = a.get("backups") or {}
        recur = sum(v[1] for v in sv.cache_hits.values())
        reclaim = dups.get("bytes", 0) + backups.get("safe_bytes", 0) + recur
        unread = sv.errors.get("권한 없음", 0)
        return {"files": len(sv.files), "total_bytes": sv.total_bytes,
                "reclaim": reclaim, "unread": unread}


class HeavyGate:
    """'여기부터 오래 걸립니다' 동의 창을 스레드 사이에서 처리합니다.

    일꾼 스레드는 여기서 멈춰 기다리고, 화면(주 스레드)이 답을 넣어 줍니다.
    """

    def __init__(self):
        self._event = threading.Event()
        self._go = True

    def ask(self, worker, estimate_sec):
        self._event.clear()
        worker.emit("heavy", estimate=estimate_sec)
        self._event.wait()               # 화면이 answer() 를 부를 때까지 대기
        return self._go

    def answer(self, go):
        self._go = go
        self._event.set()


# ─────────────────────────────────────────────────────────────
#  화면
# ─────────────────────────────────────────────────────────────

def gb(n):
    return f"{n / 2**30:,.1f} GB"


def comma(n):
    return f"{n:,}"


class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("맥 파일 상태 조사")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._center(WIN_W, WIN_H)

        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.heavy_gate = HeavyGate()
        self.worker = None
        self.start_time = 0
        self._last_left = ""
        self._shown = {}                 # 부드러운 숫자 변화를 위해 마지막 값 기억
        self._pulsing = False

        self._make_start()
        # 시작하자마자 자가진단 (APP_UI 8항). 실패하면 시작 버튼을 막습니다.
        self.after(50, self._run_selftest)
        self.after(100, self._drain_queue)

    # ── 공통 ────────────────────────────────────────────────
    def _center(self, w, h):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _clear(self):
        for w in self.winfo_children():
            w.destroy()

    def _card(self, parent, pad=14):
        c = tk.Frame(parent, bg=CARD, highlightbackground=LINE,
                     highlightthickness=1)
        return c

    # ─────────────────────────────────────────────────────────
    #  ① 시작 화면 (APP_UI 2항)
    # ─────────────────────────────────────────────────────────
    def _make_start(self):
        self._clear()
        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True, padx=28, pady=22)

        tk.Label(root, text="맥 파일 상태 조사", font=FONT_BIG,
                 bg=BG, fg=INK).pack(pady=(10, 2))
        tk.Label(root, text="참여해 주셔서 감사합니다 🙏", font=FONT,
                 bg=BG, fg=SUB).pack(pady=(0, 12))

        # 무엇을 왜 하는지 — 앱 설명
        intro = ("디자이너의 맥에는 몇 년간 작업 파일이 쌓입니다. 저희는 이걸 "
                 "AI가 분석해서 자동으로 정리하고 용량을 확보해 주는 프로그램을 "
                 "만들고 있어요.\n\n"
                 "그 프로그램을 잘 만들려면 실제 디자이너 맥북의 '파일 상태' "
                 "데이터가 필요합니다. 이 앱이 그 정보를 조사해 문서 하나로 "
                 "정리해 드립니다.")
        tk.Label(root, text=intro, font=FONT_SM, bg=BG, fg=INK,
                 justify="left", wraplength=WIN_W - 72).pack(fill="x", pady=(0, 12))

        # 익명화·안전 안내 (누르기 전에 읽는 안심 문구)
        card = self._card(root)
        card.pack(fill="x", pady=(0, 8))
        for line in ("· 파일을 하나도 바꾸지 않습니다 (읽기만 해요)",
                     "· 지우거나 옮기지 않습니다",
                     "· 모든 내용은 익명 처리됩니다 — 파일 이름은 안 들어가요",
                     "· 파일 종류·용량 같은 통계만 문서로 정리됩니다"):
            tk.Label(card, text=line, font=FONT_SM, bg=CARD, fg=INK,
                     anchor="w", wraplength=WIN_W - 104,
                     justify="left").pack(fill="x", padx=16, pady=3)
        tk.Frame(card, bg=CARD, height=2).pack()

        # 권한 안내 (APP_UI 6항) — 미리 알립니다
        tk.Label(root,
                 text="※ '문서/바탕화면 폴더에 접근하려 합니다' 창이 뜨면 [허용] 을 "
                      "눌러 주세요. 안 누르면 그 폴더는 못 셉니다.",
                 font=FONT_SM, bg=BG, fg=SUB, justify="left",
                 wraplength=WIN_W - 72).pack(pady=(8, 10))

        self.start_btn = tk.Button(root, text="  조사 시작  ", font=FONT_H,
                                   command=self._start, relief="flat",
                                   bg=ACCENT, fg="white",
                                   activebackground=INK, activeforeground="white",
                                   padx=18, pady=8, cursor="pointinghand")
        self.start_btn.pack(pady=(2, 4))

        # 예상 시간은 버튼 아래 (눌러도 되는지 판단한 뒤 보는 정보)
        self.est_label = tk.Label(
            root, text="약 1분 걸립니다 (파일이 많으면 더 오래 걸릴 수 있어요)",
            font=FONT_SM, bg=BG, fg=SUB)
        self.est_label.pack()

        self.selftest_label = tk.Label(root, text="", font=FONT_SM, bg=BG, fg=SUB)
        self.selftest_label.pack(pady=(6, 0))

    def _run_selftest(self):
        """옛 파일이면 잘못된 결과가 나오므로, 통과해야 시작할 수 있습니다."""
        try:
            bad = engine.selftest()
        except Exception as e:
            bad = -1
            self.selftest_label.config(text=f"자가진단 오류: {e}", fg=WARN)
        if bad == 0:
            self.selftest_label.config(
                text=f"✓ 자가진단 통과 · 최신 파일입니다 ({engine.VERSION})", fg=OK)
        else:
            self.start_btn.config(state="disabled", bg=LINE)
            self.selftest_label.config(
                text="⚠ 자가진단 실패 — 옛 파일입니다. 다시 받아 주세요.", fg=WARN)

    # ─────────────────────────────────────────────────────────
    #  ② 진행 화면 (APP_UI 3항) ★ 핵심
    # ─────────────────────────────────────────────────────────
    def _make_progress(self):
        self._clear()
        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True, padx=28, pady=22)

        self.step_label = tk.Label(root, text="시작하는 중…", font=FONT_H,
                                   bg=BG, fg=INK, anchor="w")
        self.step_label.pack(fill="x", pady=(4, 12))

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("P.Horizontal.TProgressbar", troughcolor=CARD,
                        background=ACCENT, bordercolor=LINE, thickness=14)
        self.bar = ttk.Progressbar(root, style="P.Horizontal.TProgressbar",
                                   mode="indeterminate", length=WIN_W - 56)
        self.bar.pack(fill="x")
        self.bar.start(14)
        self._pulsing = True

        self.time_label = tk.Label(root, text="0초 경과", font=FONT_SM,
                                   bg=BG, fg=SUB, anchor="w")
        self.time_label.pack(fill="x", pady=(8, 14))

        # 찾은 것 — 위에서부터 한 줄씩 채워집니다
        card = self._card(root)
        card.pack(fill="x")
        tk.Label(card, text="찾은 것", font=FONT_SM, bg=CARD, fg=SUB,
                 anchor="w").pack(fill="x", padx=16, pady=(10, 4))
        self.found_rows = {}
        for key, label in (("files", "파일"), ("total_bytes", "용량"),
                           ("families", "버전 가족"), ("dup_bytes", "똑같은 파일"),
                           ("recur_bytes", "다시 생기는 것")):
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", padx=16, pady=2)
            tk.Label(row, text=label, font=FONT, bg=CARD, fg=INK,
                     anchor="w").pack(side="left")
            val = tk.Label(row, text="—", font=FONT_NUM, bg=CARD, fg=INK,
                           anchor="e")
            val.pack(side="right")
            self.found_rows[key] = val
        tk.Frame(card, bg=CARD, height=8).pack()

        # 멈추기 — 위험색이 아닙니다 (읽기만 하므로 잃는 게 없음)
        self.stop_btn = tk.Button(root, text="멈추기", font=FONT,
                                  command=self._stop, relief="flat",
                                  bg=CARD, fg=INK, activebackground=LINE,
                                  highlightbackground=LINE, highlightthickness=1,
                                  padx=16, pady=4, cursor="pointinghand")
        self.stop_btn.pack(side="right", pady=(14, 0))

        self._tick_clock()

    def _tick_clock(self):
        """1초마다 '경과'를 갱신합니다 — 진행이 멈춘 게 아님을 보여줍니다."""
        if not self.worker:
            return
        el = int(time.time() - self.start_time)
        txt = f"{engine.fmt_dur(el)} 경과"
        if self._last_left:
            txt += f" · 약 {self._last_left} 남음"
        self.time_label.config(text=txt)
        self.after(1000, self._tick_clock)

    # ─────────────────────────────────────────────────────────
    #  숫자를 부드럽게 (APP_UI 3항 — 0.2초)
    # ─────────────────────────────────────────────────────────
    def _set_found(self, key, target, is_bytes):
        old = self._shown.get(key, 0)
        self._shown[key] = target
        steps = 8
        widget = self.found_rows.get(key)
        if not widget:
            return

        def frame(i):
            if not widget.winfo_exists():
                return
            v = old + (target - old) * i / steps
            widget.config(text=gb(v) if is_bytes else comma(int(round(v))))
            if i < steps:
                self.after(25, lambda: frame(i + 1))
        frame(1)

    # ─────────────────────────────────────────────────────────
    #  큐 처리 — 일꾼이 보낸 것을 주 스레드에서 화면에 반영
    # ─────────────────────────────────────────────────────────
    def _drain_queue(self):
        try:
            while True:
                kind, info = self.q.get_nowait()
                self._handle(kind, info)
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

    def _handle(self, kind, info):
        if kind == "step":
            self.step_label.config(text=info["label"])
            # 새 단계 시작 — 진행률이 없을 수 있으니 우선 물결(pulse)로.
            if not self._pulsing:
                self.bar.config(mode="indeterminate")
                self.bar.start(14)
                self._pulsing = True
        elif kind == "tick":
            if self._pulsing:
                self.bar.stop()
                self.bar.config(mode="determinate")
                self._pulsing = False
            self.bar["value"] = info.get("pct", 0)
            self._last_left = engine.fmt_dur(info.get("left", 0))
        elif kind == "found":
            self._set_found("files", info["files"], False)
            self._set_found("total_bytes", info["total_bytes"], True)
            self._set_found("families", info["families"], False)
            self._set_found("dup_bytes", info["dup_bytes"], True)
            self._set_found("recur_bytes", info["recur_bytes"], True)
        elif kind == "heavy":
            self._ask_heavy(info.get("estimate", 0))
        elif kind == "done":
            self._make_done(info)
        elif kind == "stopped":
            self._make_stopped(info)
        elif kind == "error":
            self._make_error(info.get("message", ""))

    # ─────────────────────────────────────────────────────────
    #  오래 걸리는 단계 동의 창 (APP_UI 3항)
    # ─────────────────────────────────────────────────────────
    def _ask_heavy(self, estimate_sec):
        dlg = tk.Toplevel(self)
        dlg.title("")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        w, h = 440, 300
        x = self.winfo_x() + (WIN_W - w) // 2
        y = self.winfo_y() + (WIN_H - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        when = f"약 {engine.fmt_dur(estimate_sec)} 예상." if estimate_sec else "수십 분이 걸릴 수 있습니다."
        tk.Label(dlg, text="여기부터 오래 걸립니다", font=FONT_H,
                 bg=BG, fg=INK).pack(pady=(22, 12))
        body = (f"파일 안을 하나씩 읽습니다. {when}\n\n"
                "백신이 깔려 있으면 더 걸릴 수 있습니다.\n"
                "(파일을 처음 열 때마다 검사가 붙습니다)\n\n"
                "중간에 멈추셔도 읽은 것은 저장됩니다.")
        tk.Label(dlg, text=body, font=FONT, bg=BG, fg=INK,
                 justify="center").pack(padx=20)

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(side="bottom", pady=18)

        def choose(go):
            dlg.grab_release()
            dlg.destroy()
            self.heavy_gate.answer(go)

        # '여기까지만' 이 정식 선택지입니다 (1단계만으로도 쓸 만함).
        tk.Button(btns, text="여기까지만", font=FONT, relief="flat",
                  bg=CARD, fg=INK, highlightbackground=LINE, highlightthickness=1,
                  padx=16, pady=6, cursor="pointinghand",
                  command=lambda: choose(False)).pack(side="left", padx=8)
        tk.Button(btns, text="계속하기", font=FONT, relief="flat",
                  bg=ACCENT, fg="white", activebackground=INK,
                  padx=18, pady=6, cursor="pointinghand",
                  command=lambda: choose(True)).pack(side="left", padx=8)
        # 창을 닫으면 '여기까지만' 으로 봅니다 (안전한 쪽).
        dlg.protocol("WM_DELETE_WINDOW", lambda: choose(False))

    # ─────────────────────────────────────────────────────────
    #  ③ 완료 화면 (APP_UI 4항)
    # ─────────────────────────────────────────────────────────
    def _make_done(self, info):
        self.worker = None
        self._clear()
        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True, padx=28, pady=20)

        self._check_mark(root)
        tk.Label(root, text="스캔 완료!", font=FONT_BIG,
                 bg=BG, fg=INK).pack(pady=(4, 10))

        card = self._card(root)
        card.pack(fill="x")
        self._done_row(card, "파일", comma(info["files"]) + " 개")
        self._done_row(card, "용량", gb(info["total_bytes"]))
        self._done_row(card, "정리할 수 있는 것", gb(info["reclaim"]))
        tk.Frame(card, bg=CARD, height=6).pack()

        # 못 읽은 것을 숨기지 않습니다 (APP_UI 6항 · DECISIONS 7항)
        if info.get("unread"):
            tk.Label(root,
                     text=f"⚠ {info['unread']}곳을 못 읽었습니다 (권한 없음)\n"
                          "   시스템 설정 → 개인정보 보호 및 보안 → 전체 디스크 접근",
                     font=FONT_SM, bg=BG, fg=WARN, justify="left").pack(pady=(8, 0))

        self.report_path = info["report"]
        tk.Label(root, text="보고서 파일이 바탕화면에 만들어졌어요.", font=FONT,
                 bg=BG, fg=INK).pack(pady=(12, 0))
        tk.Label(root, text=os.path.basename(self.report_path), font=FONT_SM,
                 bg=BG, fg=SUB).pack(pady=(0, 2))
        tk.Label(root, text="이 파일을 개발자에게 보내 주세요!", font=FONT_H,
                 bg=BG, fg=ACCENT).pack(pady=(2, 8))

        self._open_buttons(root, primary=True)

        # 감사·보상 (보내주시면 이렇게 보답드려요)
        thanks = self._card(root)
        thanks.pack(fill="x", pady=(12, 0))
        tk.Label(thanks,
                 text="도움 주셔서 정말 감사합니다 🙏\n"
                      "프로그램이 완성되면 무료로 쓰실 수 있게 해 드리고,\n"
                      "스타벅스 기프티콘도 보내 드릴게요! ☕",
                 font=FONT_SM, bg=CARD, fg=INK, justify="center",
                 wraplength=WIN_W - 104).pack(fill="x", padx=16, pady=10)

        tk.Label(root,
                 text="보내시기 전에 열어서 확인해 보셔도 됩니다. "
                      "파일 이름은 들어 있지 않아요.",
                 font=FONT_SM, bg=BG, fg=SUB, justify="center",
                 wraplength=WIN_W - 72).pack(pady=(10, 0))

    def _check_mark(self, parent):
        """완료 표시 — 원 세 겹 + 흰 체크 (APP_UI 4항)."""
        cv = tk.Canvas(parent, width=64, height=64, bg=BG, highlightthickness=0)
        cv.pack(pady=(10, 0))
        rings = [(4, "#CDEBDD"), (12, "#8FD3B4"), (20, OK)]
        for inset, color in rings:
            cv.create_oval(inset, inset, 64 - inset, 64 - inset,
                           fill=color, outline=color)
        cv.create_line(22, 33, 29, 41, fill="white", width=4,
                       capstyle="round")
        cv.create_line(29, 41, 44, 24, fill="white", width=4,
                       capstyle="round")

    # ─────────────────────────────────────────────────────────
    #  멈춤 화면 (APP_UI 5항)
    # ─────────────────────────────────────────────────────────
    def _make_stopped(self, info):
        self.worker = None
        self._clear()
        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True, padx=28, pady=24)

        tk.Label(root, text="멈췄습니다", font=FONT_BIG, bg=BG, fg=INK).pack(
            anchor="w", pady=(16, 14))
        tk.Label(root,
                 text="지금까지 읽은 것은 저장했습니다.\n"
                      "다시 실행하면 그 부분은 건너뜁니다.",
                 font=FONT, bg=BG, fg=INK, justify="left").pack(anchor="w")

        self.report_path = info["report"]
        tk.Label(root, text="\n여기까지의 결과도 바탕화면에 저장했습니다.", font=FONT,
                 bg=BG, fg=INK, justify="left").pack(anchor="w")
        tk.Label(root, text=os.path.basename(self.report_path), font=FONT_SM,
                 bg=BG, fg=SUB).pack(anchor="w", pady=(2, 4))
        tk.Label(root,
                 text="여기까지만이라도 개발자에게 보내 주시면 큰 도움이 됩니다!",
                 font=FONT, bg=BG, fg=ACCENT, justify="left").pack(
                     anchor="w", pady=(0, 16))

        self._open_buttons(root, primary=True, with_close=True)

    # ─────────────────────────────────────────────────────────
    #  오류 화면 (실패를 숨기지 않습니다)
    # ─────────────────────────────────────────────────────────
    def _make_error(self, message):
        self.worker = None
        self._clear()
        root = tk.Frame(self, bg=BG)
        root.pack(fill="both", expand=True, padx=28, pady=24)
        tk.Label(root, text="검사 중 문제가 생겼습니다", font=FONT_H,
                 bg=BG, fg=INK).pack(anchor="w", pady=(20, 10))
        tk.Label(root, text=message, font=FONT_SM, bg=BG, fg=WARN,
                 wraplength=WIN_W - 64, justify="left").pack(anchor="w")
        tk.Label(root, text="\n못 읽었으면 못 읽었다고 알려 드립니다.\n"
                            "화면을 닫고 다시 시도해 주세요.",
                 font=FONT, bg=BG, fg=SUB, justify="left").pack(anchor="w")
        tk.Button(root, text="처음으로", font=FONT, relief="flat", bg=ACCENT,
                  fg="white", padx=16, pady=6, cursor="pointinghand",
                  command=self._make_start).pack(anchor="w", pady=18)

    # ── 공통 버튼 ──────────────────────────────────────────
    def _done_row(self, card, label, value):
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=16, pady=4)
        tk.Label(row, text=label, font=FONT, bg=CARD, fg=INK,
                 anchor="w").pack(side="left")
        tk.Label(row, text=value, font=FONT_NUM, bg=CARD, fg=INK,
                 anchor="e").pack(side="right")

    def _open_buttons(self, parent, primary=False, with_close=False):
        bar = tk.Frame(parent, bg=BG)
        bar.pack()
        # 결과 열어보기 가 주 버튼 (확인하고 보내라는 뜻)
        tk.Button(bar, text="결과 열어보기", font=FONT, relief="flat",
                  bg=ACCENT if primary else CARD, fg="white" if primary else INK,
                  activebackground=INK, padx=16, pady=6, cursor="pointinghand",
                  command=self._open_report).pack(side="left", padx=6)
        tk.Button(bar, text="폴더에서 보기", font=FONT, relief="flat",
                  bg=CARD, fg=INK, highlightbackground=LINE, highlightthickness=1,
                  padx=16, pady=6, cursor="pointinghand",
                  command=self._reveal_report).pack(side="left", padx=6)
        if with_close:
            tk.Button(bar, text="닫기", font=FONT, relief="flat", bg=CARD, fg=INK,
                      highlightbackground=LINE, highlightthickness=1,
                      padx=16, pady=6, cursor="pointinghand",
                      command=self.destroy).pack(side="left", padx=6)

    def _open_report(self):
        engine.subprocess.run(["open", self.report_path])

    def _reveal_report(self):
        engine.subprocess.run(["open", "-R", self.report_path])

    # ── 흐름 제어 ──────────────────────────────────────────
    def _start(self):
        self.stop_flag.clear()
        self._shown = {}
        self._last_left = ""
        self.start_time = time.time()
        self._make_progress()
        self.worker = SurveyWorker(self.q, self.stop_flag, self.heavy_gate)
        self.worker.start()

    def _stop(self):
        self.stop_flag.set()
        # 오래 걸리는 단계 동의 창이 떠 있다면 풀어 줍니다.
        self.heavy_gate.answer(False)
        self.stop_btn.config(text="멈추는 중…", state="disabled")


def main():
    # ── 옛 Tk 경고 ───────────────────────────────────────────
    #   macOS 에 딸린 시스템 Tk(8.5)는 유명한 버그가 있습니다:
    #   버튼은 그려지는데 글자·카드·프로그레스바가 안 그려집니다.
    #   (Xcode·시스템 파이썬이 이 옛 Tk 를 씁니다.)
    #   python.org 정식 파이썬(Tk 8.6)으로 돌리면 정상입니다.
    #   빌드된 .app 은 빌드에 쓴 파이썬의 Tk 를 담으므로, 빌드도
    #   python.org 파이썬으로 하면 동료들 화면도 정상입니다.
    if tk.TkVersion < 8.6:
        msg = (f"화면 엔진(Tk)이 옛 버전입니다: {tk.TkVersion}\n\n"
               "이 버전은 글자·카드가 안 보이는 버그가 있어요.\n"
               "python.org 에서 최신 Python 3 을 설치해\n"
               "새 터미널에서 다시 실행해 주세요 (Tk 8.6).")
        print("\n  ⚠ " + msg.replace("\n", "\n    ") + "\n", flush=True)
        try:
            from tkinter import messagebox
            r = tk.Tk(); r.withdraw()
            messagebox.showwarning("옛 화면 엔진(Tk 8.5)", msg)
            r.destroy()
        except Exception:
            pass
        # 그래도 실행은 해 봅니다(경고만 하고 막지는 않습니다).

    App().mainloop()


if __name__ == "__main__":
    main()
