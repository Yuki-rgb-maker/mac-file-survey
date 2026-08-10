#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0 실측 — 무엇을 먼저 만들지 결정하기 위한 조사 도구

이 프로그램은 파일을 하나도 바꾸지 않습니다.
읽기만 하고, 마지막에 보고서 파일 하나만 씁니다.

    python3 p0_survey.py                 # 기본 (홈 폴더)
    python3 p0_survey.py --quick         # 빠르게 (해시 검사 생략)
    python3 p0_survey.py --path ~/작업    # 특정 폴더만
    python3 p0_survey.py --json out.json # 원시 수치도 저장

주의: 보고서와 채점표에 실제 파일 이름이 들어갑니다.
     본인 맥에서 본인이 보려고 만든 도구입니다. 공유하거나 커밋하지 마세요.
     (남에게 돌리는 배포용은 이 도구를 실제로 써 본 뒤에 따로 만듭니다)
"""

import os
import re
import sys
import json
import time
import math
import struct
import shutil
import hashlib
import tempfile
import argparse
import platform
import subprocess
import unicodedata
from collections import defaultdict, Counter

VERSION = "p0-26"

# ─────────────────────────────────────────────────────────────
#  언어 사전
#
#  언어에 묶인 것(버전 낱말·문자 대역·업무 단서·문장)은 전부
#  lang_ko.py 에 있습니다. 이 파일(엔진)은 언어를 몰라야 합니다.
#
#  나중에 다른 언어를 넣을 때는 lang_ja.py 를 만들어 바꿔 끼웁니다.
#  지금은 언어가 하나뿐이라 인터페이스를 미리 설계하지 않습니다
#  ("추상화는 두 번째 사례가 나왔을 때").
# ─────────────────────────────────────────────────────────────
try:
    import lang_ko as LANG
except ImportError:
    print("\n  lang_ko.py 가 없습니다.")
    print("  p0_survey.py 와 같은 폴더에 두어야 합니다.\n")
    raise SystemExit(1)

# ─────────────────────────────────────────────────────────────
# 0. 단계 기록기
#
#   왜 필요한가:
#     한 번 "남의 작업물 신호 찾는 중…" 에서 10분 넘게 멈춘 적이 있는데,
#     어느 단계가 몇 초 걸렸는지 아무 데도 안 적혀 있어서
#     원인을 짐작으로 고쳤고, 결국 엉뚱한 곳을 손봤습니다.
#
#   그래서 이건 옵션이 아니라 항상 켜져 있습니다.
#     옵션으로 두면 정작 필요할 때 안 켜고 돌립니다.
#
#   같이 하는 일:
#     · 단계마다 걸린 시간과 처리한 개수를 기록
#     · 단계가 끝날 때마다 중간 결과를 파일에 이어 씀
#       (Ctrl+C 로 멈춰도 거기까지는 남습니다)
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
#  화면으로 진행 상황을 넘기는 통로
#
#  터미널에서 돌릴 때는 아무 일도 안 합니다(print 로 충분).
#  앱(p0_app.py)에서 부를 때만 여기에 함수를 꽂아, 진행 막대와
#  남은 시간을 화면에 표시합니다.
#
#  이렇게 분리하는 이유:
#    검사 로직이 화면을 몰라야 합니다. 1편에서 "화면은 판단하지
#    않는다" 로 정한 것과 같은 이유입니다.
# ─────────────────────────────────────────────────────────────

PROGRESS_HOOK = None        # fn(kind, **info) — 앱이 꽂습니다


def _notify(kind, **info):
    """진행 상황을 화면에 알립니다. 화면이 없으면 아무 일도 안 합니다."""
    if PROGRESS_HOOK:
        try:
            PROGRESS_HOOK(kind, **info)
        except Exception:
            # 화면 쪽 문제로 검사가 멈추면 안 됩니다.
            # 다만 조용히 넘기지 않고 터미널에는 남깁니다.
            print("    (화면 갱신 실패 — 검사는 계속합니다)")


class ReadCache:
    """파일에서 파낸 것을 저장해 둡니다. 같은 파일을 두 번 읽지 않습니다.

    왜 필요한가:
      이 맥에서 큰 파일을 **처음** 열면 백신이 전체를 검사하느라
      8GB 짜리에서 768KB 읽는 데 18분이 걸렸습니다.
      두 번째부터는 검사 결과가 캐시돼 0ms 였습니다.
      (같은 파일 60개: 1차 3,932초 → 2차 0.0초)

      즉 비용은 **파일마다 딱 한 번** 입니다. 그러니 한 번 읽은 것을
      버리지 않으면 됩니다. 3시간 읽다가 Ctrl+C 로 멈춰도
      그때까지 읽은 것은 남아야 합니다.

    제품(2·3편)의 인벤토리 DB 와 같은 구조입니다.
    여기서 만들어 보고 그대로 옮깁니다.

    파일이 바뀌었는지는 (크기, 수정시각) 으로 판별합니다.
    내용을 다시 읽어 확인하면 캐시의 의미가 없어지니까요.
    """

    def __init__(self, path="p0_cache.json", enabled=True):
        self.path = path
        self.enabled = enabled
        self.data = {}
        self.hits = 0
        self.misses = 0
        self.dirty = 0
        if enabled:
            self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
            print(f"  이전에 읽어 둔 것 {len(self.data):,}개를 씁니다"
                  f" ({self.path})", flush=True)
        except (OSError, ValueError) as e:
            # 조용히 넘기지 않습니다. 캐시가 깨졌으면 알아야 다시 읽습니다.
            print(f"  (읽어 둔 것을 못 불러왔습니다: {e} — 처음부터 읽습니다)")
            self.data = {}

    @staticmethod
    def _key(path, size, mtime):
        # 크기나 수정시각이 바뀌면 다른 키가 되어 자동으로 다시 읽습니다
        return f"{path}|{size}|{int(mtime)}"

    def get(self, path, size, mtime):
        if not self.enabled:
            self.misses += 1
            return None
        v = self.data.get(self._key(path, size, mtime))
        if v is None:
            self.misses += 1
        else:
            self.hits += 1
        return v

    def put(self, path, size, mtime, value):
        if not self.enabled:
            return
        self.data[self._key(path, size, mtime)] = value
        self.dirty += 1
        # 200개마다 저장합니다. 매번 쓰면 느리고, 안 쓰면 멈췄을 때 날아갑니다.
        if self.dirty >= 200:
            self.save()

    def save(self):
        if not self.enabled or not self.dirty:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)      # 쓰다 멈춰도 원본이 안 깨지게
            self.dirty = 0
        except OSError as e:
            print(f"  (읽어 둔 것을 저장하지 못했습니다: {e})")

    def summary(self):
        total = self.hits + self.misses
        if not total:
            return ""
        return (f"{self.hits:,}개는 저장해 둔 것을 썼고 "
                f"{self.misses:,}개를 새로 읽었습니다 "
                f"({self.hits / total * 100:.0f}% 절약)")


class Progress:
    """진행률과 남은 시간을 보여 줍니다.

    왜 필요한가:
      느린 단계에서 화면이 안 움직이면 멈춘 것처럼 보입니다.
      실제로 "10분 지났는데 이 상태" 라는 보고를 받고, 원인을 짐작해
      읽는 양을 줄였다가 **정작 필요한 정보를 못 읽게** 된 적이 있습니다.

      필요한 걸 안 읽고 빨라지는 건 의미가 없습니다.
      느린 건 느린 대로 두되, 얼마나 남았는지 알려주는 쪽이 맞습니다.

    남은 시간 계산:
      지금까지 처리한 개수와 걸린 시간으로 개당 속도를 내고,
      남은 개수를 곱합니다. 파일 크기가 제각각이라 정확하진 않지만
      "멈춘 게 아니라 4분쯤 남았구나" 를 알기엔 충분합니다.
    """

    def __init__(self, total, every=None, label=""):
        self.total = max(total, 1)
        self.every = every or max(1, min(200, total // 20))
        self.label = label
        self.started = time.time()
        self.last_print = 0

    def tick(self, i):
        """i번째를 처리하기 직전에 부릅니다."""
        if i == 0 or i % self.every:
            return
        el = time.time() - self.started
        if el - self.last_print < 1.0:      # 1초에 한 번까지만 찍습니다
            return
        self.last_print = el
        pct = i / self.total * 100
        left = (el / i) * (self.total - i)
        print(f"    {i:,}/{self.total:,} ({pct:4.1f}%)  "
              f"{fmt_dur(el)} 경과 · 약 {fmt_dur(left)} 남음", flush=True)
        _notify("tick", done=i, total=self.total, pct=pct,
                elapsed=el, left=left, label=self.label)

    def estimate_total(self, sample_n, sample_sec):
        """표본 몇 개를 돌려본 시간으로 전체 소요를 추정합니다."""
        if sample_n <= 0:
            return 0
        return sample_sec / sample_n * self.total


def fmt_dur(sec):
    """초를 사람이 읽을 형태로."""
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}초"
    if sec < 3600:
        return f"{sec//60}분 {sec%60}초"
    return f"{sec//3600}시간 {(sec%3600)//60}분"


def ask_continue(step_name, estimate_sec, count, auto_yes=False):
    """예상 시간을 알리고 계속할지 물어봅니다.

    왜 묻는가:
      필요한 걸 다 읽으면 오래 걸립니다. 몰래 줄이는 대신
      **얼마나 걸릴지 알려주고 사용자가 정하게** 합니다.

      그리고 이건 제품의 첫 스캔 화면과 같은 구조입니다 —
      예상 시간 고지 → 진행 표시 → 사용자가 시작. 여기서 한 번
      만들어 보면 2편에 그대로 씁니다.

    --yes 를 주면 묻지 않고 진행합니다 (자동 실행용).
    """
    print(f"\n  '{step_name}' 은 {count:,}개를 읽습니다. "
          f"약 {fmt_dur(estimate_sec)} 걸릴 것 같습니다.")
    if auto_yes:
        print("  (--yes 라서 그대로 진행합니다)\n")
        return True
    try:
        ans = input("  계속할까요? [Enter=예 / s=이 단계 건너뛰기] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  건너뜁니다.")
        return False
    if ans.startswith("s"):
        print("  건너뜁니다.\n")
        return False
    print()
    return True


def run_with_estimate(log, name, targets, work_fn, sample=40, auto_yes=False,
                      note="", ask_over=45):
    """느린 단계를 '재보고 → 알려주고 → 물어보고 → 돌린다' 순서로 실행합니다.

    왜 이런 순서인가:
      필요한 걸 다 읽으면 오래 걸립니다. 그렇다고 몰래 줄이면
      정작 필요한 정보를 못 읽게 됩니다 (실제로 그랬습니다 —
      계보 표본을 1,500개로 줄였다가, 그러면 회수량을 계산할 수 없다는
      걸 알고 되돌렸습니다).

      그래서 **표본 몇 개로 속도를 재서 전체 소요를 추정하고,
      사용자에게 알린 뒤 결정하게** 합니다.

      이건 제품(2편)의 첫 스캔 화면과 같은 구조입니다.
      예상 시간 고지 → 진행 표시 → 사용자가 시작.

    ⚠ 표본을 앞에서 40개만 뽑았더니 "39시간 걸립니다" 가 나온 적이 있습니다.
      대상이 크기순으로 정렬돼 있어서 앞쪽 40개가 전부 GB 급 영상이었고,
      그 속도로 6,000개를 곱한 것입니다. 실제로는 10분이었습니다.

      → 표본을 **전체에서 고르게** 뽑고, 곱하기 대신 **크기 비율**로
        환산합니다. 시간은 파일 크기에 거의 비례하기 때문입니다
        (백신이 파일 전체를 검사하므로).

    work_fn(progress) 가 실제 작업을 합니다. 건너뛰면 None 을 돌려줍니다.
    """
    n = len(targets)
    if n == 0:
        return None

    # 전체에서 고르게 뽑습니다 (앞에서만 뽑으면 큰 것만 걸립니다)
    probe_n = min(sample, n)
    step = n / probe_n
    picked = [targets[int(i * step)] for i in range(probe_n)]

    def size_of(item):
        try:
            return item[1] if isinstance(item, (list, tuple)) else 0
        except (IndexError, TypeError):
            return 0

    t0 = time.time()
    read_bytes = 0
    for item in picked:
        path = item[0] if isinstance(item, (list, tuple)) else item
        try:
            with open(path, "rb") as f:
                f.read(64 << 10)
            read_bytes += size_of(item)
        except OSError:
            continue
    elapsed = time.time() - t0

    # 크기 비율로 환산합니다. 표본이 전체의 몇 %를 차지하는지 보고,
    # 그 비율만큼 시간을 늘립니다.
    total_bytes = sum(size_of(t) for t in targets) or 1
    if read_bytes > 0:
        estimate = elapsed * (total_bytes / read_bytes)
    else:
        estimate = elapsed / max(probe_n, 1) * n
    estimate *= 1.5          # 실제 작업은 표본 읽기보다 조금 무겁습니다

    # 터무니없는 값이 나오면 그대로 믿지 않습니다.
    if estimate > 6 * 3600:
        print(f"\n  ('{name}' 예상치가 {fmt_dur(estimate)} 로 나왔는데,")
        print("   표본이 치우쳤을 수 있어 그대로 믿지 않습니다. 진행합니다)")
    elif estimate > ask_over and not ask_continue(name, estimate, n, auto_yes):
        log.steps.append((name + " (건너뜀)", 0.0, 0, "사용자가 건너뜀"))
        return None

    prog = Progress(n, label=name)
    return log.run(name, lambda: work_fn(prog), note=note)


class StepLog:
    """단계마다 걸린 시간과 개수를 기록합니다.

    옵션이 아니라 항상 켜져 있습니다. 옵션으로 두면 정작 필요할 때
    안 켜고 돌리게 됩니다. 단계가 끝날 때마다 파일에 이어 쓰므로
    Ctrl+C 로 멈춰도 거기까지는 남습니다.
    """

    def __init__(self, path="p0_steps.log"):
        self.path = path
        self.steps = []          # (이름, 초, 개수, 비고)
        self.started = time.time()
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(f"# P0 단계 기록  {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        except OSError as e:
            print(f"  (단계 기록 파일을 못 만들었습니다: {e})")

    def run(self, name, fn, note=""):
        """단계 하나를 돌리고 시간을 잽니다. 결과를 그대로 돌려줍니다."""
        print(f"{name}…", flush=True)
        _notify("step", name=name, index=len(self.steps))
        t0 = time.time()
        result = fn()
        el = time.time() - t0

        # 처리 개수를 눈치껏 알아냅니다 (있으면 표시, 없으면 생략)
        # 처리 개수를 눈치껏 알아냅니다.
        #   ⚠ Counter 도 dict 라서, 낱말 빈도표에서 result.get("count") 를
        #     꺼내면 "count 라는 낱말이 몇 번 나왔나" 가 나옵니다.
        #     실제로 '낱말 빈도 2개' 라고 찍힌 적이 있습니다. 그래서 제외합니다.
        n = None
        if isinstance(result, Counter):
            n = len(result)
        elif isinstance(result, dict):
            for key in ("checked", "ok", "count", "groups_checked"):
                v = result.get(key)
                if isinstance(v, int):
                    n = v
                    break
        elif isinstance(result, (list, tuple)):
            n = len(result)

        self.steps.append((name, el, n, note))
        tail = f"  {n:,}개" if isinstance(n, int) else ""
        per = f"  (개당 {el/n*1000:.0f}ms)" if isinstance(n, int) and n > 0 else ""
        print(f"    └ {el:.1f}초{tail}{per}", flush=True)
        _notify("step_done", name=name, seconds=el, count=n)
        self._append(f"{name}\t{el:.2f}s\t{n if n is not None else '-'}\t{note}")
        return result

    def _append(self, line):
        """단계 하나가 끝날 때마다 이어 씁니다.

        기록에 실패해도 검사 자체는 계속합니다. 다만 조용히 넘기지 않고
        화면에 알립니다 — 나중에 로그를 봤는데 한 줄이 비어 있으면
        '안 돌았나' 하고 헷갈리게 되니까요.
        """
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"    (단계 기록을 못 남겼습니다: {e})")

    def report_lines(self):
        """보고서에 넣을 표. 느린 순서가 아니라 실행 순서로 보여 줍니다."""
        L = ["", "── 단계별 소요 시간 ─────────────────────────────────",
             "  다음에 느릴 때 짐작하지 않고 여기를 보고 고칩니다."]
        total = sum(s[1] for s in self.steps)
        for name, el, n, note in self.steps:
            share = el / total * 100 if total else 0
            cnt = f"{n:>7,}개" if isinstance(n, int) else " " * 9
            per = (f"  개당 {el/n*1000:6.0f}ms"
                   if isinstance(n, int) and n > 0 else "")
            bar = "█" * int(share / 4)
            L.append(f"  {name:<22}{el:7.1f}초 {share:4.0f}% {bar:<25}"
                     f"{cnt}{per}")
        L.append(f"  {'합계':<22}{total:7.1f}초")
        slowest = max(self.steps, key=lambda s: s[1], default=None)
        if slowest and total and slowest[1] / total > 0.4:
            L.append("")
            L.append(f"  → '{slowest[0]}' 하나가 전체의 "
                     f"{slowest[1]/total*100:.0f}%를 씁니다. "
                     "고칠 곳이 있다면 여기입니다.")
        return L



#    클라우드·앱 라이브러리·개발 폴더는 아예 훑지 않습니다.
#    (건드리면 안 되는 곳은 세지도 않는 게 안전합니다)
# ─────────────────────────────────────────────────────────────

SKIP_DIR_NAMES = {
    "node_modules", ".git", ".svn", "venv", ".venv", "__pycache__",
    "Library", "Applications", ".Trash", ".cache", ".npm", ".cargo",
    "DerivedData", ".gradle", ".m2", "vendor", ".next", "dist", "build",
}

# 폴더처럼 보이지만 사실 파일 하나인 것들 (번들/패키지)
BUNDLE_SUFFIXES = (
    ".photoslibrary", ".fcpbundle", ".imovielibrary", ".tvlibrary",
    ".aplibrary", ".lrlibrary", ".lrcat", ".musiclibrary",
    ".app", ".framework", ".bundle", ".plugin", ".kext",
    ".sketch", ".rcproject", ".logicx", ".band", ".pkg",
)

# 클라우드 동기화 폴더 (이름으로 판별)
CLOUD_MARKERS = (
    "Library/Mobile Documents",     # iCloud Drive
    "Library/CloudStorage",         # Dropbox/Drive/OneDrive (Tahoe 이후)
    "Dropbox", "Google Drive", "OneDrive", "Box Sync", "pCloud Drive",
    "Creative Cloud Files",
)

# ─────────────────────────────────────────────────────────────
# 2. 확장자 분류
# ─────────────────────────────────────────────────────────────

SOURCE_EXTS = {  # 작업 원본 — 버전이 쌓이는 것들
    ".psd", ".psb", ".ai", ".indd", ".idml", ".afdesign", ".afphoto",
    ".afpub", ".sketch", ".fig", ".xd", ".procreate", ".clip", ".xcf",
    ".blend", ".c4d", ".ma", ".mb", ".max", ".3ds", ".obj", ".fbx",
    ".prproj", ".aep", ".fcpxml", ".drp", ".veg", ".kra",
    ".docx", ".pptx", ".xlsx", ".hwp", ".key", ".pages", ".numbers",
    ".sketchup", ".skp", ".dwg", ".dxf", ".rvt",
}

EXPORT_EXTS = {  # 내보낸 결과물 — "이 버전이 쓰였다"는 증거
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff",
    ".pdf", ".mp4", ".mov", ".gifv", ".svg", ".eps", ".heic",
}

RASTER_EXTS = {  # 썸네일을 뽑을 수 있는 것
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff",
    ".heic", ".bmp", ".psd", ".pdf",
}

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mts", ".mxf", ".r3d", ".braw"}

# [항목 7] 외부 파일을 참조하는 포맷
#   이런 파일은 자기 안에 "다른 파일의 경로"를 들고 있습니다.
#   그 대상을 옮기거나 지우면 이 파일이 깨집니다.
#   개수가 많으면 참조 그래프(refgraph)를 일찍 만들어야 한다는 뜻입니다.
REFERENCE_HOLDER_EXTS = {
    ".psd", ".psb", ".ai", ".indd", ".idml",          # 링크 이미지
    ".prproj", ".aep", ".fcpxml", ".drp", ".veg",     # 영상 프로젝트 → 원본 클립
    ".blend", ".c4d", ".ma", ".mb", ".max",           # 3D → 텍스처·링크
    ".sldasm", ".iam", ".3dm", ".skp", ".dwg",        # 어셈블리 → 파트
    ".tex", ".html", ".css", ".xcodeproj",            # 상대경로 참조
}

# [항목 24] 어도비가 아닌 작업 원본
#   이 비율이 높으면 XMP 계보에만 의존할 수 없다는 뜻이고,
#   보편 지문(지각 해시)을 먼저 만들어야 한다는 근거가 됩니다.
NON_ADOBE_SOURCE_EXTS = {
    ".afdesign", ".afphoto", ".afpub",       # Affinity
    ".sketch", ".fig", ".xd",                # Sketch / Figma / XD
    ".procreate", ".clip", ".kra", ".xcf",   # Procreate / CSP / Krita / GIMP
    ".blend", ".c4d", ".ma", ".mb", ".max",  # 3D
    ".skp", ".3dm", ".f3d", ".f3z",          # SketchUp / Rhino / Fusion
}

# [항목 26] 프로그램이 스스로 만드는 백업
#   사용자가 만든 버전이 아니라 앱이 자동 생성한 직전 저장본입니다.
#   → 판단이 쉽고(사용자 의도가 없음), 용량이 크고(원본 크기 × 개수),
#     위험이 낮습니다(원본이 옆에 있음). 3편의 첫 기능 후보입니다.
BACKUP_EXT_RE = re.compile(
    r"^\.(?:blend\d+|3dmbak|bak|bak\d+|sv\$|dwl\d?|asv|autosave|"
    r"tmp|temp|old|orig|swp|~)$", re.IGNORECASE)
BACKUP_NAME_RE = re.compile(r"^(?:~\$|\.~lock\.|Backup of )")

# [항목 27·28] 3D 포맷
#   교환 포맷(STL·OBJ·STEP)은 텍스트거나 단순 바이너리라 파싱이 쉽고,
#   네이티브 포맷(.blend·.3dm)은 썸네일이 들어 있습니다.
#   어느 쪽 비중이 큰지에 따라 3D 대응의 시작점이 달라집니다.
THREED_NATIVE_EXTS = {".blend", ".3dm", ".skp", ".f3d", ".f3z", ".c4d",
                      ".ma", ".mb", ".max", ".sldprt", ".sldasm", ".dwg", ".ztl"}
THREED_EXCHANGE_EXTS = {".stl", ".obj", ".step", ".stp", ".iges", ".igs",
                        ".3mf", ".ply", ".fbx", ".gltf", ".glb", ".dae", ".usdz"}

# [항목 10] 한글이 들어간 파일 이름인지
#   한국어 비율이 높으면 쿼리 해석과 이름 정규화를 한국어 기준으로 설계해야 하고,
#   로컬 LLM도 한국어가 강한 모델(Qwen 계열)을 골라야 합니다.
#   가-힣      합쳐진 글자 (NFC)
#   ㄱ-ㅎㅏ-ㅣ   호환용 자모 (U+3131~)
#   \u1100-\u11FF  조합용 자모 (U+1100~) ← macOS 가 쓰는 NFD 형태
#
#   ⚠ 마지막 대역이 빠져 있어서, NFD 로 저장된 한글 파일명이 전부
#     '한글 아님' 으로 세어졌습니다. 실측에서 한글 비율이 0.4% 로
#     나왔는데 실제로는 훨씬 높았습니다. NFC 로 바꾸는 게 근본 해결이지만
#     어디선가 빠질 수 있어 여기서도 방어합니다.
HANGUL_RE = LANG.SCRIPT_RE          # 이 언어의 글자가 있는가

# ─────────────────────────────────────────────────────────────
# 3. 영상·디자인 캐시 (다시 생기는 것 — 3편의 공짜 용량)
# ─────────────────────────────────────────────────────────────

# ⚠ 이 폴더들은 대부분 ~/Library 안에 있습니다. 훑기(walk)는 Library를
#   통째로 건너뛰므로 여기서 절대 경로로 직접 확인해야 합니다.
#   한 번 이걸 훑기에 맡겼다가 캐시 용량이 0으로 나온 적이 있습니다.
CACHE_ABS_PATHS = [
    ("Adobe 미디어 캐시",      "~/Library/Caches/Adobe/Common/Media Cache"),
    ("Adobe 미디어 캐시 파일",  "~/Library/Caches/Adobe/Common/Media Cache Files"),
    ("After Effects 캐시",   "~/Library/Caches/Adobe/After Effects"),
    ("Photoshop 캐시",       "~/Library/Caches/Adobe/Adobe Photoshop"),
    ("Xcode 빌드 산출물",     "~/Library/Developer/Xcode/DerivedData"),
    ("Xcode 기기 지원",       "~/Library/Developer/Xcode/iOS DeviceSupport"),
    ("DaVinci 캐시",         "~/Library/Application Support/Blackmagic Design/"
                             "DaVinci Resolve/CacheClip"),
    ("DaVinci 갤러리",       "~/Movies/.gallery"),
    ("Final Cut 백업",       "~/Movies/Final Cut Backups"),
    ("Capture One 캐시",     "~/Library/Caches/com.captureone.captureone16"),
    # [항목 30] Fusion 360 로컬 캐시
    #   Fusion 은 클라우드 네이티브라 진짜 파일과 버전 이력이 서버에 있습니다.
    #   로컬에 있는 건 캐시뿐이므로 "건드리면 안 되는 곳"이 아니라
    #   "지워도 되는 곳"입니다. 용량이 크면 관문 규칙에 넣을 근거가 됩니다.
    ("Fusion 360 캐시",      "~/Library/Application Support/Autodesk/"
                             "Autodesk Fusion 360/API/AddIns"),
    ("Fusion 360 로컬캐시",   "~/Library/Application Support/Autodesk/"
                             "Autodesk Fusion 360/Cache"),
    ("Autodesk 캐시",        "~/Library/Caches/Autodesk"),
]

# 작업 폴더 안에 섞여 있는 것들 — 훑기 중에 이름으로 잡습니다
CACHE_DIR_NAMES = {
    "Adobe Premiere Pro Video Previews": "Premiere 미리보기",
    "Adobe Premiere Pro Audio Previews": "Premiere 오디오",
    "Previews.lrdata": "Lightroom 프리뷰",
    "Smart Previews.lrdata": "Lightroom 스마트프리뷰",
    "Motion Templates": "Motion 템플릿",
    "Render Files": "렌더 파일",
    "Transcoded Media": "변환 미디어",
    "Proxy Media": "프록시 미디어",
    "Backups.backupdb": "Time Machine 로컬",
}

# ─────────────────────────────────────────────────────────────
# 4. 버전 이름 정규화 — "진짜최종.psd" 와 "최종_v2.psd" 를 한 가족으로
# ─────────────────────────────────────────────────────────────

# 한국어는 띄어쓰기 없이 붙습니다 ("진짜최종", "최종본수정")
# → 경계를 요구하지 않고 어디서든 벗겨냅니다.
# ⚠ 두 글자 미만이거나 다른 낱말 안에 흔히 들어가는 토큰은 넣지 마세요.
#   '안'을 넣었더니 '제안서'가 '제서'가 된 적이 있습니다.
# 버전 표시를 걷어내는 규칙 — 낱말 목록은 사전(lang_ko)에 있습니다.
#   한국어는 띄어쓰기 없이 붙어서(진짜최종) 경계를 요구하지 않고,
#   영어는 경계를 요구합니다(finality 의 final 을 벗기면 안 되므로).
_KO_RE = re.compile("(?:" + "|".join(LANG.VERSION_TOKENS) + ")")
_EN_RE = re.compile(
    r"(?:^|[\s_\-.·()]+)(?:" + "|".join(LANG.VERSION_TOKENS_BOUNDED)
    + r")(?=$|[\s_\-.·()])", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?:^|[\s_\-.·()]+)(?:" + "|".join(LANG.DATE_TOKENS)
    + r")(?=$|[\s_\-.·()])")

# 기계가 붙인 이름은 뒤의 숫자·날짜가 '고유 번호' 라 벗기면 안 됩니다.
#   목록은 사전(lang_ko.MACHINE_PREFIXES)에 있습니다.
_MACHINE_RE = re.compile(
    r"^(" + "|".join(LANG.MACHINE_PREFIXES) + r")", re.IGNORECASE)

_BARE_NUM_RE = re.compile(r"(?:^|[\s_\-.·]+)\d{1,3}(?=$|[\s_\-.·])")
_TRIM_RE = re.compile(r"^[\s_\-.·()]+|[\s_\-.·()]+$")


def normalize_stem(stem: str) -> str:
    """버전 표시를 걷어낸 이름의 뼈대를 돌려줍니다.

    카메라 접두어(IMG_4821)는 뒤 숫자를 남깁니다 — 서로 다른 사진이므로.
    """
    # 기계가 붙인 이름(IMG_4821, 스크린샷 2024-05-02 오후 3.50)은
    # 숫자와 날짜가 '버전 표시' 가 아니라 '그 파일의 정체' 입니다. 남깁니다.
    # 밖에서 이미 NFC 로 바꿔 주지만, 다른 경로로 들어온 이름도 있어
    # 여기서 한 번 더 통일합니다. 이미 NFC 면 비용이 거의 없습니다.
    stem = LANG.normalize_text(stem)
    machine = bool(_MACHINE_RE.match(stem.strip()))
    s = stem
    for _ in range(8):                      # 겹쳐 붙은 것을 반복해서 벗김
        new = _EN_RE.sub("", s)
        new = _KO_RE.sub("", new)
        if not machine:
            new = _DATE_RE.sub("", new)
            new = _BARE_NUM_RE.sub("", new)
        new = _TRIM_RE.sub("", new)
        if new == s:
            break
        s = new
    s = re.sub(r"[\s_\-.·]+", " ", s).strip().lower()

    # 안전장치: 너무 많이 깎이면 엉뚱한 파일들이 한 가족이 됩니다.
    # 원본이 충분히 길었는데 한 글자만 남았다면 정규화를 포기합니다.
    if len(s) < 2 and len(stem.strip()) >= 4:
        return re.sub(r"[\s_\-.·]+", " ", stem).strip().lower()
    return s


# ─────────────────────────────────────────────────────────────
# 5. 훑기
# ─────────────────────────────────────────────────────────────

class Survey:
    """[항목 1·3·4·5] 폴더를 훑어 파일 목록과 '건너뛴 것'을 셉니다.

    핵심은 마지막 항목입니다 — 클라우드·앱 라이브러리·개발 폴더를 빼고 나면
    실제로 손댈 수 있는 파일이 몇 %인지가 나옵니다. 이 비율이 낮으면
    홈 폴더 전체를 스캔하는 설계 자체가 낭비라는 뜻입니다.
    """

    def __init__(self, root, quick=False, max_files=400_000):
        self.root = os.path.expanduser(root)
        self.quick = quick
        self.max_files = max_files

        self.files = []              # (path, size, mtime, ext, stem)
        # 생성 시각은 macOS 에만 있습니다(st_birthtime). files 튜플을 늘리면
        # 이걸 쓰는 함수를 전부 고쳐야 해서, 별도 사전에 담습니다.
        self.btimes = {}
        self.total_bytes = 0
        self.skipped_dirs = Counter()
        self.errors = Counter()      # 실패는 숨기지 않고 셉니다
        self.error_samples = []
        self.cloud_bytes = 0
        self.cloud_files = 0
        self.bundle_count = 0
        self.bundle_bytes = 0
        self.cache_hits = defaultdict(lambda: [0, 0])   # 이름 -> [개수, 바이트]
        self.hit_limit = False

    # ── 걸러내기 ────────────────────────────────────────────
    def _is_cloud(self, path):
        return any(m in path for m in CLOUD_MARKERS)

    def _is_bundle(self, name):
        low = name.lower()
        return low.endswith(BUNDLE_SUFFIXES)

    def _cache_label(self, name):
        return CACHE_DIR_NAMES.get(name)

    def scan_known_caches(self):
        """~/Library 등 훑지 않는 곳의 캐시를 절대 경로로 직접 확인합니다."""
        for label, p in CACHE_ABS_PATHS:
            full = os.path.expanduser(p)
            if os.path.isdir(full):
                n, b = dir_size(full)
                self.cache_hits[label][0] += n
                self.cache_hits[label][1] += b

    # ── 본체 ───────────────────────────────────────────────
    def walk(self):
        stack = [self.root]
        while stack:
            if len(self.files) >= self.max_files:
                self.hit_limit = True
                break
            cur = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except PermissionError:
                self.errors["권한 없음"] += 1
                if len(self.error_samples) < 8:
                    self.error_samples.append(("권한 없음", cur))
                continue
            except OSError as e:
                self.errors[f"읽기 실패({e.errno})"] += 1
                if len(self.error_samples) < 8:
                    self.error_samples.append((f"errno {e.errno}", cur))
                continue

            for e in entries:
                name = e.name
                if name.startswith("._"):          # 리소스 포크
                    continue
                path = e.path

                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                    is_link = e.is_symlink()
                except OSError:
                    self.errors["상태 확인 실패"] += 1
                    continue

                if is_link:                        # 심볼릭 링크는 따라가지 않음
                    continue

                # 캐시 폴더는 용량만 재고 안으로 안 들어감
                label = self._cache_label(name)
                if label and is_dir:
                    n, b = dir_size(path)
                    self.cache_hits[label][0] += n
                    self.cache_hits[label][1] += b
                    continue

                if is_dir:
                    if self._is_bundle(name):
                        self.bundle_count += 1
                        _, b = dir_size(path)
                        self.bundle_bytes += b
                        continue
                    if name in SKIP_DIR_NAMES:
                        self.skipped_dirs[name] += 1
                        continue
                    if name.startswith(".") and name not in (".",):
                        self.skipped_dirs["숨김폴더"] += 1
                        continue
                    if self._is_cloud(path):
                        n, b = dir_size(path)
                        self.cloud_files += n
                        self.cloud_bytes += b
                        continue
                    stack.append(path)
                    continue

                # 파일
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    self.errors["크기 확인 실패"] += 1
                    continue

                # ⚠ macOS 는 한글 파일명을 자모로 쪼개서(NFD) 저장합니다.
                #   '스크린샷' 이 ㅅ+ㅡ+ㅋ+ㅡ+ㄹ+ㅣ+ㄴ+ㅅ+ㅑ+ㅅ 으로 들어옵니다.
                #   우리 정규식은 합쳐진 형태(NFC)로 쓰여 있어서 안 맞습니다.
                #   실제로 이것 때문에 스크린샷 265장이 한 가족으로 묶였습니다.
                #   (컨테이너에서는 NFC 라 시험을 통과해서 못 잡았습니다)
                stem, ext = os.path.splitext(LANG.normalize_text(name))
                ext = ext.lower()
                self.files.append((path, st.st_size, st.st_mtime, ext, stem))
                bt = getattr(st, "st_birthtime", None)
                if bt:
                    self.btimes[path] = bt
                self.total_bytes += st.st_size


def dir_size(path, cap=200_000):
    """폴더 하나의 개수·용량. 안에 들어가지 않을 곳을 재는 용도."""
    n = 0
    b = 0
    stack = [path]
    while stack and n < cap:
        cur = stack.pop()
        try:
            for e in os.scandir(cur):
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        n += 1
                        b += e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        except OSError:
            continue
    return n, b


# ─────────────────────────────────────────────────────────────
# 6. 분석
# ─────────────────────────────────────────────────────────────

def cluster_versions(files, min_size=1024):
    """[항목 17 비교군] 파일 이름만으로 버전 가족을 묶습니다.

    이 결과는 그 자체가 목적이 아니라 '계보로 묶은 것'과 비교하기 위한
    대조군입니다. 이름이 놓치는 게 얼마나 되는지가 3편의 존재 이유입니다.
    """
    groups = defaultdict(list)
    for path, size, mtime, ext, stem in files:
        if size < min_size:
            continue
        key = (os.path.dirname(path), normalize_stem(stem), ext)
        if not key[1]:
            continue
        groups[key].append((path, size, mtime, stem))

    families = [(k, v) for k, v in groups.items() if len(v) >= 2]
    families.sort(key=lambda kv: -sum(f[1] for f in kv[1]))
    return families


def find_derivatives(files):
    """[보조 증거] 작업 원본 옆에 내보낸 결과물이 있는 이름이 몇 개인가.

    ⚠ 이건 '확실'이 아니라 '보조' 증거입니다. 이름을 안 바꾸고 재내보내거나,
      중간 버전을 확인용으로 내보내거나, 데스크탑에 내보내는 일이 흔합니다.
      특히 파일명 관리가 엉망인 사람일수록 마지막 경우가 심합니다.
      그래서 계보(XMP)가 있으면 그쪽을 우선합니다.
    """
    by_key = defaultdict(set)
    for path, size, mtime, ext, stem in files:
        key = (os.path.dirname(path), normalize_stem(stem))
        if key[1]:
            by_key[key].add(ext)

    with_source = 0
    with_both = 0
    for key, exts in by_key.items():
        if exts & SOURCE_EXTS:
            with_source += 1
            if exts & EXPORT_EXTS:
                with_both += 1
    return with_source, with_both


def head_hash(path, chunk=16 << 10):
    """[1차] 파일 앞 16KB 만 읽어 요약값을 만듭니다.

    점프(seek)가 없어 순차 읽기라 빠릅니다.
    같은 크기 400개 중 진짜 중복이 20개라면, 이 단계에서 대부분이 갈라지고
    2차(뒤쪽까지 읽기)로 넘어가는 건 20개 남짓이 됩니다.
    """
    try:
        with open(path, "rb") as f:
            return hashlib.blake2b(f.read(chunk), digest_size=8).digest()
    except OSError:
        return None


def tail_hash(path, size, chunk=64 << 10):
    """[2차] 파일 끝 64KB 까지 읽어 확정에 가깝게 만듭니다.

    ⚠ 여기에 점프가 들어갑니다. 2GB 파일이면 2GB 를 건너뛰어야 하고,
      파일이 디스크에 조각나 있으면 더 걸립니다. 그래서 1차를 통과한
      것에만 씁니다.

    ⚠ 앞만 읽으면 안 되는 이유:
      같은 프로그램·같은 캔버스로 만든 PSD 는 머리말이 상당히 비슷해서
      앞부분만으로는 서로 다른 파일이 같아 보입니다. 영상도 마찬가지고요
      (코덱·해상도 정보가 앞에 몰려 있음). 뒷부분은 실제 내용의 끝이라
      훨씬 잘 갈립니다.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    try:
        with open(path, "rb") as f:
            h.update(f.read(chunk))
            if size > chunk * 2:
                f.seek(-chunk, os.SEEK_END)
                h.update(f.read(chunk))
    except OSError:
        return None
    return h.digest()


def find_duplicates(files, quick=False, min_size=1 << 20, progress=None,
                    cache=None):
    """[항목 9] 내용이 100% 같은 파일이 몇 GB 인가.

    ※ 이건 우리 상품이 아닙니다 — CleanMyMac 등이 이미 잘 합니다.
      규모 기준선으로만 씁니다. 다만 3D 에셋(같은 텍스처가 프로젝트마다 복사)
      에서는 예외적으로 앞세울 만해서 함께 셉니다.

    ⚠ 이 값은 '같다'의 증거가 아닙니다.
      크기와 앞뒤 일부가 같다는 뜻일 뿐입니다. P0 는 '대략 몇 GB 냐' 를
      재는 게 목적이라 이대로 쓰지만, 제품에서 실제로 지울 때는
      후보를 좁힌 뒤 전체를 읽어 확인하는 단계가 반드시 필요합니다.
      지우는 건 되돌릴 수 없으니 '거의 확실히 같다' 로 지우면 안 됩니다.

    ── 걸러내는 순서 (좁은 것부터, 싼 것부터) ──────────────
      0차  크기가 같은가        파일을 안 열어도 됨            공짜
      1차  앞 16KB 가 같은가    순차 읽기, 점프 없음           쌈
      2차  뒤 64KB 도 같은가    점프가 들어감                  비쌈

    ⚠ 속도 주의 — 여기서 두 번 멈춘 적이 있습니다.
      ① 64KB 이상 파일 전부를 열어 앞뒤를 읽다가 수만 번의 디스크 왕복이
         일어났습니다. → 하한 1MB, 회수량 큰 묶음부터, 60초 제한으로 고침
      ② 제한을 묶음 사이에서만 확인해서, 파일 수천 개짜리 묶음 하나에
         갇혀 60초 제한이 169초가 됐습니다. → 묶음 안에서도 확인
      ③ 그래도 169초가 걸려서 1·2차로 나눴습니다. 대부분은 1차에서 갈리고,
         비싼 2차는 살아남은 것에만 돌아갑니다.
    """
    if quick:
        return {"files": 0, "bytes": 0, "samples": [], "groups_checked": 0,
                "groups_total": 0, "stopped_early": False,
                "min_size": min_size, "stage1": 0, "stage2": 0,
                "unreadable": 0, "unreadable_samples": []}

    by_size = defaultdict(list)
    meta = {}
    for path, size, mtime, ext, stem in files:
        if size >= min_size:
            by_size[size].append(path)
            meta[path] = mtime

    # 짝이 있는 묶음만, 회수 가능한 용량이 큰 순서로
    groups = [(size, paths) for size, paths in by_size.items() if len(paths) >= 2]
    groups.sort(key=lambda g: -(g[0] * (len(g[1]) - 1)))

    dup_files = dup_bytes = 0
    samples = []
    checked = stage1_reads = stage2_reads = 0
    unreadable = []            # ⚠ 못 읽은 파일. 조용히 버리지 않습니다
    stopped_early = False
    total_files = sum(len(p) for _, p in groups)
    prog = progress or Progress(total_files)

    for size, paths in groups:
        checked += 1

        # ── 1차: 앞 16KB 로 후보 좁히기 ──────────────────
        by_head = defaultdict(list)
        for p in paths:
            prog.tick(stage1_reads)
            stage1_reads += 1
            ck = f"h1:{p}"
            hv = cache.get(ck, size, meta.get(p, 0)) if cache else None
            if hv is None:
                h = head_hash(p)
                hv = h.hex() if h else False
                if cache:
                    cache.put(ck, size, meta.get(p, 0), hv)
            if hv:
                by_head[hv].append(p)
            else:
                # 권한이 없거나 잠긴 파일. 목록에서 사라지면
                # "중복이 없다" 로 잘못 읽히므로 따로 셉니다.
                unreadable.append(p)

        # ── 2차: 1차를 통과한 묶음만 뒤쪽까지 ─────────────
        for hv, cand in by_head.items():
            if len(cand) < 2:
                continue                      # 혼자 남았으면 중복이 아님
            by_full = defaultdict(list)
            for p in cand:
                stage2_reads += 1
                ck = f"h2:{p}"
                fv = cache.get(ck, size, meta.get(p, 0)) if cache else None
                if fv is None:
                    t = tail_hash(p, size)
                    fv = t.hex() if t else False
                    if cache:
                        cache.put(ck, size, meta.get(p, 0), fv)
                if fv:
                    by_full[fv].append(p)
                else:
                    unreadable.append(p)
            for fv, group in by_full.items():
                if len(group) >= 2:
                    extra = len(group) - 1
                    dup_files += extra
                    dup_bytes += size * extra
                    # 등급 계산에 쓰려면 전부 필요합니다.
                    # (보고서에는 위에서 몇 개만 보여 줍니다)
                    samples.append((size, group))

    return {"files": dup_files, "bytes": dup_bytes,
            "samples": sorted(samples, key=lambda s: -s[0])[:2000],
            "groups_checked": checked, "groups_total": len(groups),
            "stopped_early": stopped_early, "min_size": min_size,
            "stage1": stage1_reads, "stage2": stage2_reads,
            "unreadable": len(unreadable),
            "unreadable_samples": unreadable[:5]}


# 이름 품질 판정 — 규칙은 사전(lang_ko.BAD_NAME_PATTERNS)에 있습니다.
BAD_NAME_RES = [(re.compile(p, re.I), label)
                for p, label in LANG.BAD_NAME_PATTERNS]


def name_quality(files):
    """[항목 8] 이름이 얼마나 엉망인가 — 무제·기계이름·자모·'최종'류 분포.

    이 비율이 높으면 ① 2편의 '애매한 파일 보관함'이 커지고
    ② 이름을 못 믿으니 계보와 겉모습 지문이 그만큼 중요해집니다.
    """
    c = Counter()
    for path, size, mtime, ext, stem in files:
        s = stem.strip()
        matched = False
        for rx, label in BAD_NAME_RES:
            if rx.search(s):
                c[label] += 1
                matched = True
                break
        if not matched:
            c["정상"] += 1
    return c


def age_buckets(files, now=None):
    """[항목 6] 얼마나 오래 안 건드린 파일인가 (수정 시각 기준).

    2편의 '1년 이상 미사용 파일 일괄 정리' 대상이 몇 개인지를 봅니다.

    ⚠ 한계: 수정 시각이지 '마지막으로 연 시각'이 아닙니다.
      실제 제품에서는 Spotlight 의 kMDItemLastUsedDate 를 써야 정확합니다
      (5년 전에 만들었어도 지난달에 열었다면 살아있는 파일입니다).
    """
    now = now or time.time()
    year = 365 * 86400
    b = Counter()
    sz = Counter()
    for path, size, mtime, ext, stem in files:
        age = (now - mtime) / year
        if age < 1:
            k = "1년 이내"
        elif age < 2:
            k = "1~2년"
        elif age < 3:
            k = "2~3년"
        elif age < 5:
            k = "3~5년"
        else:
            k = "5년 이상"
        b[k] += 1
        sz[k] += size
    return b, sz


# ─────────────────────────────────────────────────────────────
# 6-b. XMP 계보 — 파일 이름을 못 믿을 때의 진짜 증거
#
#   어도비 파일은 XMP 라는 평문 XML 조각을 품고 다닙니다.
#   그 안에 "원본이 무엇이었나 / 어디서 갈라졌나 / 몇 번 저장했나"가
#   들어 있어서, 이름이 'ㅁㄴㅇㄹ.psd' 여도 족보를 알 수 있습니다.
#
#   포맷을 파싱하지 않고 바이트로 찾기 때문에 psd·ai·indd·pdf 를
#   가리지 않습니다. 앞뒤 일부만 읽으므로 2GB 파일도 즉시 끝납니다.
# ─────────────────────────────────────────────────────────────

XMP_OPEN = b"<x:xmpmeta"
XMP_CLOSE = b"</x:xmpmeta>"
XMP_PROBE_EXTS = SOURCE_EXTS | {".pdf", ".jpg", ".jpeg", ".tif", ".tiff", ".png"}

_RE_ORIG = re.compile(r'OriginalDocumentID(?:>|="|=")([^"<]+)')
_RE_DOCID = re.compile(r'(?<!Original)(?<!Instance)DocumentID(?:>|="|=")([^"<]+)')
# DerivedFrom 은 두 가지 모양으로 쓰입니다. 둘 다 잡아야 합니다.
#   속성형: <xmpMM:DerivedFrom stRef:documentID="xmp.did:..."/>
#   요소형: <xmpMM:DerivedFrom ...><stRef:documentID>xmp.did:...</stRef:documentID>
_RE_DERIVED = re.compile(
    r'DerivedFrom.{0,400}?(?:stRef:)?documentID(?:>|="|=\')([^"\'<]+)',
    re.DOTALL | re.IGNORECASE)
_RE_HIST = re.compile(r'stEvt:action')
_RE_CREATE = re.compile(r'xmp:CreateDate(?:>|="|=")([^"<]+)')
_RE_MODIFY = re.compile(r'xmp:ModifyDate(?:>|="|=")([^"<]+)')
_RE_TOOL = re.compile(r'xmp:CreatorTool(?:>|="|=")([^"<]+)')


def read_xmp(path, size, head=768 << 10, tail=256 << 10):
    """[항목 16] 파일 앞뒤 일부만 읽어 XMP 블록을 찾습니다. 없으면 None.

    ⚠ 속도 주의 — 여기서 한 번 멈춘 적이 있습니다.
      처음엔 앞 3MB + 뒤 3MB 를 읽고, 블록이 잘렸으면 12MB 를 다시
      읽었습니다. 게다가 큰 파일부터 4,000개를 도니 총 24GB 이상을
      읽는 셈이었습니다. 지난번 '남의 작업물' 과 똑같은 실수입니다.

      XMP 가 어디 있는지 알면 이렇게 많이 읽을 필요가 없습니다.
        PSD   머리말 바로 뒤 이미지 리소스 블록 → 앞쪽
        JPEG  APP1 마커 → 맨 앞
        PDF   문서 끝 쪽인 경우가 많음 → 뒤쪽
      그래서 앞 768KB + 뒤 256KB 면 대부분 잡힙니다.

      블록이 잘렸을 때 다시 읽던 예비 코드는 지웠습니다.
      드물게 놓치는 것보다 24GB 를 읽는 대가가 큽니다.
    """
    try:
        with open(path, "rb") as f:
            blob = f.read(head)
            i = blob.find(XMP_OPEN)
            if i < 0 and size > head + tail:
                f.seek(size - tail)
                blob = f.read(tail)
                i = blob.find(XMP_OPEN)
            if i < 0:
                return None
            j = blob.find(XMP_CLOSE, i)
            if j < 0:
                return None          # 잘렸으면 포기합니다 (다시 안 읽음)
            return blob[i:j + len(XMP_CLOSE)].decode("utf-8", "replace")
    except OSError:
        return None


def parse_xmp(xml):
    """[항목 16·19] XMP 문자열에서 계보 정보를 꺼냅니다.

    OriginalDocumentID 가 핵심입니다 — '다른 이름으로 저장'을 해도 안 바뀌므로,
    이름이 'ㅁㄴㅇㄹ.psd' 여도 어느 작업의 몇 번째 버전인지 알 수 있습니다.
    """
    if not xml:
        return None

    def one(rx):
        m = rx.search(xml)
        return m.group(1).strip() if m else None

    return {
        "original_id": one(_RE_ORIG),
        "doc_id": one(_RE_DOCID),
        "derived_from": one(_RE_DERIVED),
        "save_count": len(_RE_HIST.findall(xml)),
        "create": one(_RE_CREATE),
        "modify": one(_RE_MODIFY),
        "tool": one(_RE_TOOL),
    }


def probe_lineage(files, limit=6000, progress=None, cache=None):
    """[항목 16·19] XMP 계보가 몇 %에 있고, 평균 저장 횟수가 얼마인가.

    - 16번(존재 비율): 파일 이름을 안 믿고도 가족을 묶을 수 있는지 가름합니다
    - 19번(저장 횟수): '이 파일을 얼마나 붙들고 있었나'의 직접 측정값입니다
      3번 저장한 파일과 80번 저장한 파일은 다른 물건입니다
    """
    # 큰 것부터 읽습니다.
    #   한때 "고르게 뽑는 게 맞다" 며 바꿨다가 되돌렸습니다.
    #   회수 가능한 용량은 큰 파일에서 나오고, 그 용량을 계산하려면
    #   큰 파일의 계보를 알아야 합니다. 작은 파일을 고르게 읽으면
    #   'XMP 가 몇 %에 있나' 는 알아도 '몇 GB 가 회수되나' 는 모릅니다.
    #   그게 ★★★ 숫자이므로 큰 것부터가 맞습니다.
    targets = [f for f in files if f[3] in XMP_PROBE_EXTS]
    targets.sort(key=lambda f: -f[1])
    targets = targets[:limit]

    found = 0
    with_orig = 0
    with_derived = 0
    saves = []
    tools = Counter()
    by_orig = defaultdict(list)
    checked = 0
    errors = 0

    prog = progress or Progress(len(targets))
    for i, (path, size, mtime, ext, stem) in enumerate(targets):
        prog.tick(i)
        checked += 1

        # 저장해 둔 게 있으면 파일을 열지 않습니다.
        #   이 맥에서 큰 파일 첫 읽기는 백신 검사 때문에 수십 초가 걸립니다.
        #   두 번째 실행에서 그 비용을 다시 낼 이유가 없습니다.
        info = cache.get(path, size, mtime) if cache else None
        if info is None:
            xml = read_xmp(path, size)
            info = parse_xmp(xml) if xml is not None else False
            if cache:
                cache.put(path, size, mtime, info)
        if info is False or info is None:
            continue
        if not info:
            errors += 1
            continue
        found += 1
        if info["original_id"]:
            with_orig += 1
            by_orig[info["original_id"]].append((path, size, mtime, info))
        if info["derived_from"]:
            with_derived += 1
        if info["save_count"]:
            saves.append(info["save_count"])
        if info["tool"]:
            tools[info["tool"][:40]] += 1

    fams = {k: v for k, v in by_orig.items() if len(v) >= 2}
    return {
        "checked": checked,
        "found": found,
        "with_orig": with_orig,
        "with_derived": with_derived,
        "save_avg": (sum(saves) / len(saves)) if saves else 0,
        "save_max": max(saves) if saves else 0,
        "tools": tools,
        "lineage_families": fams,
        "parse_errors": errors,
        "stopped_early": False,
    }


def compare_family_methods(name_families, lineage_families):
    """이름으로 묶은 것과 계보로 묶은 것이 얼마나 다른지.

    ★ 이 숫자가 3편의 존재 이유입니다.
      계보가 이름이 놓친 가족을 많이 찾아낸다면, 파일명을 못 믿는
      사용자에게 우리가 줄 수 있는 게 확실히 있다는 뜻입니다.
    """
    name_paths = set()
    for _, members in name_families:
        for m in members:
            name_paths.add(m[0])

    lin_paths = set()
    for _, members in lineage_families.items():
        for m in members:
            lin_paths.add(m[0])

    return {
        "name_only": len(name_paths - lin_paths),
        "lineage_only": len(lin_paths - name_paths),   # ★ 이름이 놓친 것
        "both": len(name_paths & lin_paths),
        "name_families": len(name_families),
        "lineage_families": len(lineage_families),
    }


# ─────────────────────────────────────────────────────────────
# 6-c. PSD 안쪽 — 색공간과 레이어 (헤더만 읽습니다)
#
#   ⚠ 레이어가 많다 = 나중 버전, 이 직관은 틀립니다.
#     최종본일수록 병합해서 레이어가 줄어듭니다.
#     그래서 개수보다 '색공간'과 '저장 횟수'가 더 좋은 신호입니다.
# ─────────────────────────────────────────────────────────────

PSD_MODES = {0: "비트맵", 1: "회색", 2: "인덱스", 3: "RGB",
             4: "CMYK", 7: "멀티채널", 8: "듀오톤", 9: "Lab"}


def read_psd_header(path):
    """[항목 20] PSD 헤더 26바이트만 읽어 폭·높이·채널·색공간을 꺼냅니다."""
    try:
        with open(path, "rb") as f:
            h = f.read(26)
        if len(h) < 26 or h[:4] != b"8BPS":
            return None
        import struct
        sig, ver, _, chans, height, width, depth, mode = struct.unpack(
            ">4sH6sHIIHH", h)
        return {
            "w": width, "h": height, "channels": chans,
            "depth": depth, "mode": PSD_MODES.get(mode, str(mode)),
            "psb": ver == 2,
        }
    except (OSError, Exception):
        return None


def probe_psd(files, limit=2000):
    """[항목 18·20] PSD 헤더를 읽어 색공간 분포를 냅니다.

    CMYK 비율이 왜 중요한가: RGB 로 작업하다 CMYK 로 바꾸는 건 인쇄 넘기기
    직전에만 하는 일이라, '이 버전이 최종본'이라는 강한 신호입니다.
    (레이어 개수는 신호가 아닙니다 — 최종본일수록 병합해서 줄어듭니다)
    """
    ok = 0
    fail = 0
    modes = Counter()
    sizes = Counter()
    for path, size, mtime, ext, stem in files:
        if ext not in (".psd", ".psb"):
            continue
        if ok + fail >= limit:
            break
        info = read_psd_header(path)
        if info:
            ok += 1
            modes[info["mode"]] += 1
            sizes[f"{info['w']}x{info['h']}"] += 1
        else:
            fail += 1
    return {"ok": ok, "fail": fail, "modes": modes, "canvas": sizes}


# ─────────────────────────────────────────────────────────────
# 6-d. 항목 21·25 — 썸네일과 지각 해시
#
#   왜 재는가:
#     계보(XMP)는 어도비 파일에만 있습니다. Figma·Canva·Procreate·Blender
#     사용자에게는 없습니다. 그런 파일도 가족으로 묶으려면 "겉모습"을
#     비교해야 하고, 그러려면 썸네일이 나와야 합니다.
#
#   이 숫자가 결정하는 것:
#     썸네일 성공률이 높으면 → 보편 지문(지각 해시)을 바닥에 깔 수 있습니다.
#     낮으면 → 어도비 전용 도구가 되고, 비어도비 사용자를 못 받습니다.
#
#   방법:
#     qlmanage  macOS QuickLook. 앱이 설치돼 있으면 그 앱의 플러그인을 씁니다.
#               (Blender 는 설치 시 QuickLook 확장을 함께 깝니다)
#     sips      macOS 내장 이미지 변환기. 픽셀을 꺼내는 데 씁니다.
#
#   ※ 둘 다 외부 프로세스라 느립니다. 확장자별로 표본만 검사합니다.
# ─────────────────────────────────────────────────────────────

def quicklook_thumbnail_ok(path, workdir):
    """[항목 21] 이 파일에서 QuickLook 썸네일이 나오는가 (True/False).

    원본은 읽기만 합니다. 결과 이미지는 전용 임시 폴더에 만들고,
    다음 호출을 위해 폴더를 통째로 비우고 시작합니다.
    (개별 삭제를 try/except 로 감싸면 실패를 삼키게 되므로 그렇게 하지 않습니다)
    """
    if platform.system() != "Darwin":
        return False
    out = os.path.join(workdir, "ql")
    shutil.rmtree(out, ignore_errors=True)     # 이전 결과가 섞이지 않게
    try:
        os.makedirs(out, exist_ok=True)
        r = subprocess.run(
            ["qlmanage", "-t", "-s", "128", "-o", out, path],
            # ⚠ 12초였을 때, 플러그인이 없는 확장자에서 매번 12초씩
            #   기다리느라 전체의 61% 를 썼습니다. 4초면 충분합니다.
            capture_output=True, text=True, timeout=4)
        made = [f for f in os.listdir(out) if f.endswith(".png")]
        return bool(made) and r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _sips_to_small_bmp(path, workdir, side=9):
    """이미지를 9×9 BMP로 줄여 바이트로 돌려줍니다 (지각 해시용).

    BMP를 고른 이유: 압축이 없어서 표준 라이브러리만으로 픽셀을 읽을 수 있습니다.
    (PNG 는 zlib 해제와 필터 복원이 필요해서 코드가 길어집니다)
    """
    if platform.system() != "Darwin":
        return None
    dst = os.path.join(workdir, "ph.bmp")
    try:
        r = subprocess.run(
            ["sips", "-s", "format", "bmp", "-z", str(side), str(side),
             path, "--out", dst],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not os.path.exists(dst):
            return None
        with open(dst, "rb") as f:
            data = f.read()
        os.remove(dst)
        return data
    except (subprocess.TimeoutExpired, OSError):
        return None


def _bmp_gray_rows(data, side=9):
    """BMP 바이트에서 밝기값 격자를 꺼냅니다. 실패하면 None."""
    try:
        if data[:2] != b"BM":
            return None
        offset = struct.unpack("<I", data[10:14])[0]
        w, h = struct.unpack("<ii", data[18:26])
        bpp = struct.unpack("<H", data[28:30])[0]
        if bpp not in (24, 32) or w < side or abs(h) < side:
            return None
        step = bpp // 8
        rowsize = ((w * step + 3) // 4) * 4      # BMP 는 4바이트 정렬
        rows = []
        for y in range(side):
            base = offset + y * rowsize
            row = []
            for x in range(side):
                p = base + x * step
                b, g, r = data[p], data[p + 1], data[p + 2]
                row.append((r * 299 + g * 587 + b * 114) // 1000)   # 밝기
            rows.append(row)
        return rows
    except (struct.error, IndexError):
        return None


def dhash(path, workdir, side=9):
    """[항목 25] 겉모습 지문 64비트. 비슷하게 생긴 이미지끼리 값이 가깝습니다.

    원리: 가로로 이웃한 두 픽셀 중 어느 쪽이 밝은지만 기록합니다.
          밝기 전체가 아니라 '차이의 방향'이라 밝기·대비 변화에 강합니다.
    한계: 겉모습만 봅니다. 초안과 최종이 아예 다르게 생겼으면 못 묶습니다.
          그래서 계보가 있으면 계보가 우선입니다(증거 사다리).
    """
    data = _sips_to_small_bmp(path, workdir, side)
    if not data:
        return None
    rows = _bmp_gray_rows(data, side)
    if not rows:
        return None
    bits = 0
    n = 0
    for y in range(side - 1):
        for x in range(side - 1):
            bits |= (1 if rows[y][x] < rows[y][x + 1] else 0) << n
            n += 1
    return bits


def hamming(a, b):
    """두 지문이 몇 비트 다른가. 작을수록 닮았습니다."""
    return bin(a ^ b).count("1")


# 썸네일을 확인할 대상 — 작업 파일만.
#   ⚠ 한때 모든 확장자를 검사했습니다. 그랬더니 .py·.cs·.js 같은 코드 파일과
#     .meta·.uasset·.prefab 같은 Unity 파일이 대상의 절반을 차지했고,
#     "썸네일 성공률 59%" 라는 오해를 낳았습니다.
#     (이미지·디자인 포맷은 100% 였는데 Unity 파일이 끌어내린 것)
#
#   우리가 알고 싶은 건 "작업 파일을 눈으로 비교할 수 있는가" 입니다.
#   코드 파일의 썸네일이 나오는지는 아무 상관이 없습니다.
THUMB_TARGET_EXTS = (
    SOURCE_EXTS | RASTER_EXTS | THREED_NATIVE_EXTS |
    {".ai", ".indd", ".idml", ".pdf", ".svg", ".eps",
     ".mov", ".mp4", ".m4v", ".arw", ".cr2", ".cr3", ".nef", ".dng",
     ".heic", ".webp", ".gif"}
) - {".txt", ".md", ".json", ".xml", ".csv"}


def probe_thumbnails(files, workdir, per_ext=8, total_cap=140,
                     give_up_after=2):
    """[항목 21·29] 확장자별 썸네일 성공률.

    확장자마다 큰 파일 순으로 몇 개씩만 검사합니다.
    (모든 파일에 외부 프로세스를 돌리면 몇 시간이 걸립니다)

    ⚠ 속도 주의 — 여기가 전체의 61% 를 쓴 적이 있습니다 (1,129초 / 220개).
      원인은 **실패하는 확장자에서 타임아웃까지 기다린 것** 이었습니다.
      Unity 파일 90개가 각각 12초씩 기다려 1,080초를 썼습니다.

      고친 것:
        · 대상을 작업 파일로 한정 (코드·Unity 파일 제외)
        · 타임아웃 12초 → 4초
        · 한 확장자에서 연속 2번 실패하면 그 확장자는 포기
          (QuickLook 플러그인이 없으면 몇 개를 더 해봐도 결과가 같습니다)
    """
    by_ext = defaultdict(list)
    for f in files:
        if f[3] in THUMB_TARGET_EXTS:
            by_ext[f[3]].append(f)

    result = {}
    used = 0
    for ext, group in sorted(by_ext.items(), key=lambda kv: -len(kv[1])):
        if used >= total_cap:
            break
        group.sort(key=lambda f: -f[1])
        ok = 0
        tried = 0
        consecutive_fail = 0
        for f in group[:per_ext]:
            if used >= total_cap:
                break
            tried += 1
            used += 1
            if quicklook_thumbnail_ok(f[0], workdir):
                ok += 1
                consecutive_fail = 0
            else:
                consecutive_fail += 1
                # 플러그인이 없으면 더 해봐야 똑같습니다
                if consecutive_fail >= give_up_after:
                    break
        if tried:
            result[ext] = (ok, tried)
    return result


def probe_phash_families(files, workdir, cap=260):
    """[항목 25] 이름도 계보도 없이, 겉모습만으로 몇 가족이 묶이는가.

    이 숫자가 크면 비어도비 사용자(Figma·Canva·AI 생성)도 받을 수 있다는 뜻입니다.

    비용을 줄이는 방법:
      같은 폴더에 같은 확장자가 2개 이상 있는 경우만 후보로 삼습니다.
      혼자 있는 이미지는 어차피 가족이 될 수 없으니 검사할 이유가 없습니다.
    """
    groups = defaultdict(list)
    for f in files:
        if f[3] in RASTER_EXTS and f[1] > 20 * 1024:
            groups[(os.path.dirname(f[0]), f[3])].append(f)

    cands = []
    for g in groups.values():
        if len(g) >= 2:
            cands.extend(g)
    cands.sort(key=lambda f: -f[1])
    cands = cands[:cap]

    prints = []
    fail = 0
    for f in cands:
        h = dhash(f[0], workdir)
        if h is None:
            fail += 1
        else:
            prints.append((f[0], f[1], h))

    # 지문이 가까운 것끼리 묶기 (64비트 중 10비트 이하 차이 = 닮음)
    used = set()
    fams = []
    for i in range(len(prints)):
        if i in used:
            continue
        group = [prints[i]]
        used.add(i)
        for j in range(i + 1, len(prints)):
            if j in used:
                continue
            if hamming(prints[i][2], prints[j][2]) <= 10:
                group.append(prints[j])
                used.add(j)
        if len(group) >= 2:
            fams.append(group)

    fams.sort(key=lambda g: -sum(m[1] for m in g))
    return {"checked": len(cands), "hashed": len(prints),
            "fail": fail, "families": fams}


# ─────────────────────────────────────────────────────────────
# 6-e. 항목 22·23 — AI 생성물의 증거
#
#   왜 재는가:
#     AI 생성 이미지는 PSD 보다 메타데이터가 오히려 풍부합니다.
#     Stable Diffusion·ComfyUI 는 프롬프트·시드·모델·워크플로를 PNG 안에 넣고,
#     2026년 현재 주요 AI 툴은 C2PA 서명을 기본으로 박습니다.
#
#   이 숫자가 결정하는 것:
#     비율이 높으면 AI 크리에이터를 초기 타깃에 넣을 근거가 됩니다.
#     (같은 프롬프트 = 같은 시도, 같은 시드 = 같은 이미지의 변주 로 묶임)
#
#   주의:
#     소셜 플랫폼은 업로드·재인코딩 때 메타데이터를 벗겨냅니다.
#     카톡·인스타를 거친 파일은 이미 비어 있을 수 있어, 낮게 나와도
#     "원래 없었다"가 아니라 "거쳐 오면서 잃었다"일 수 있습니다.
# ─────────────────────────────────────────────────────────────

# 생성 파라미터가 들어가는 대표적인 키워드
AI_PARAM_KEYS = (b"parameters", b"prompt", b"workflow", b"Comment",
                 b"sd-metadata", b"Dream", b"negative_prompt")


def read_png_text_keys(path, limit=2 << 20):
    """[항목 22] PNG 안의 텍스트 조각 이름들을 돌려줍니다.

    PNG 는 [길이][이름][내용][검사값] 조각이 줄줄이 이어진 구조입니다.
    그중 tEXt·iTXt·zTXt 가 글자를 담는 조각이고,
    Stable Diffusion 계열이 여기에 프롬프트와 시드를 넣습니다.
    """
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            keys = []
            read = 8
            while read < limit:
                head = f.read(8)
                if len(head) < 8:
                    break
                size, kind = struct.unpack(">I4s", head)
                if kind == b"IEND":
                    break
                if kind in (b"tEXt", b"iTXt", b"zTXt"):
                    body = f.read(min(size, 400))
                    keys.append(body.split(b"\0")[0].decode("latin-1", "replace"))
                    f.seek(size - min(size, 400) + 4, os.SEEK_CUR)
                elif kind == b"IDAT":        # 픽셀이 시작되면 더 볼 필요 없음
                    break
                else:
                    f.seek(size + 4, os.SEEK_CUR)
                read += 8 + size + 4
            return keys
    except (OSError, struct.error):
        return None


def has_c2pa(path, size, head=1 << 20, tail=512 << 10):
    """[항목 23] C2PA(콘텐츠 자격증명) 흔적이 있는가.

    ※ 서명을 검증하지 않습니다. "있다/없다"만 봅니다.
      P0 의 목적은 진위 판별이 아니라 '이 증거원을 쓸 수 있는가' 이므로
      존재 확인으로 충분합니다. 실제 제품에서는 검증이 필요합니다.
    """
    try:
        with open(path, "rb") as f:
            blob = f.read(head)
            if b"c2pa" in blob or b"jumbf" in blob or b"caBX" in blob:
                return True
            if size > head:
                f.seek(max(0, size - tail))
                blob = f.read(tail)
                return (b"c2pa" in blob or b"jumbf" in blob or b"caBX" in blob)
    except OSError:
        return False
    return False


def probe_ai_metadata(files, limit=1200):
    """[항목 22·23] AI 생성 흔적이 있는 이미지가 몇 %인가."""
    imgs = [f for f in files if f[3] in (".png", ".jpg", ".jpeg", ".webp")]
    imgs.sort(key=lambda f: -f[1])
    imgs = imgs[:limit]

    png_checked = png_with_params = 0
    c2pa_checked = c2pa_found = 0
    key_counter = Counter()

    for i, (path, size, mtime, ext, stem) in enumerate(imgs):
        if i and i % 200 == 0:
            print(f"    …{i}/{len(imgs)}", flush=True)
        if ext == ".png":
            png_checked += 1
            keys = read_png_text_keys(path)
            if keys:
                for k in keys:
                    key_counter[k[:30]] += 1
                if any(any(a.decode("latin-1").lower() in k.lower()
                           for a in AI_PARAM_KEYS) for k in keys):
                    png_with_params += 1
        c2pa_checked += 1
        if has_c2pa(path, size):
            c2pa_found += 1

    return {"png_checked": png_checked, "png_with_params": png_with_params,
            "c2pa_checked": c2pa_checked, "c2pa_found": c2pa_found,
            "keys": key_counter}


# ─────────────────────────────────────────────────────────────
# 6-f. 항목 26·27·28·29 — 3D
#
#   왜 재는가:
#     3D 작업 파일은 크고, 3D 앱은 스스로 백업을 만듭니다.
#     자동 백업은 사용자 의도가 없는 파일이라 판단이 쉽고 위험이 낮아서,
#     3편의 3D 대응에서 가장 먼저 손댈 대상 후보입니다.
#
#   이 숫자가 결정하는 것:
#     26번이 크면 → 자동 백업 정리를 3D 첫 기능으로 확정.
#     28번이 크면 → STL·OBJ·STEP 부터 (파싱이 쉬움).
#     29번이 높으면 → .blend 는 썸네일 경로로 갈 수 있음.
# ─────────────────────────────────────────────────────────────

def probe_backups(files):
    """[항목 26] 프로그램이 자동 생성한 백업 파일의 개수와 용량.

    확장자(.blend1, .3dmbak, .bak …)와 이름(~$…)으로 판별합니다.
    '원본이 옆에 있는가'도 함께 셉니다 — 원본이 있어야 안전하게 지울 수 있습니다.
    """
    existing = {f[0] for f in files}
    hits = []
    for path, size, mtime, ext, stem in files:
        name = os.path.basename(path)
        is_backup = bool(BACKUP_EXT_RE.match(ext)) or bool(BACKUP_NAME_RE.match(name))
        if not is_backup:
            continue
        # 원본 추정: 백업 확장자를 떼면 원본 이름이 나와야 합니다.
        #   scene.blend1  → scene.blend    (숫자만 뗌)
        #   part.3dmbak   → part.3dm       ('bak'만 뗌 — 통째로 떼면 .3dm이 사라짐)
        #   file.psd.bak  → file.psd       (.bak을 뗌)
        #   ~$보고서.docx  → 보고서.docx     (앞의 표시를 뗌)
        origin = None
        low = ext.lower()
        if re.match(r"^\.blend\d+$", low):
            origin = path[: -len(ext)] + ".blend"
        elif low == ".3dmbak":
            origin = path[: -len(ext)] + ".3dm"
        elif low in (".bak", ".old", ".orig", ".autosave", ".asv", ".sv$"):
            origin = path[: -len(ext)]
        elif BACKUP_NAME_RE.match(name):
            origin = os.path.join(os.path.dirname(path),
                                  BACKUP_NAME_RE.sub("", name))
        hits.append((path, size, origin in existing if origin else None))

    total = sum(h[1] for h in hits)
    with_origin = sum(1 for h in hits if h[2] is True)
    # 원본이 옆에 있는 것만 '안전하게 회수 가능'으로 셉니다.
    # 원본이 없는 백업은 사실상 유일본이라 지우면 되돌릴 수 없습니다.
    safe_bytes = sum(h[1] for h in hits if h[2] is True)
    by_ext = Counter()
    for path, size, _ in hits:
        by_ext[os.path.splitext(path)[1].lower() or "(이름규칙)"] += size
    return {"count": len(hits), "bytes": total, "safe_bytes": safe_bytes,
            "with_origin": with_origin, "by_ext": by_ext,
            "samples": sorted(hits, key=lambda h: -h[1])[:6]}


def probe_3d(files):
    """[항목 27·28] 3D 파일이 얼마나, 어떤 종류로 있는가."""
    native = Counter()
    exchange = Counter()
    nbytes = ebytes = 0
    for path, size, mtime, ext, stem in files:
        if ext in THREED_NATIVE_EXTS:
            native[ext] += 1
            nbytes += size
        elif ext in THREED_EXCHANGE_EXTS:
            exchange[ext] += 1
            ebytes += size
    return {"native": native, "exchange": exchange,
            "native_bytes": nbytes, "exchange_bytes": ebytes}


def read_blend_header(path):
    """[항목 29] .blend 파일의 머리말을 읽고 썸네일 유무를 확인합니다.

    .blend 구조: 'BLENDER' + 포인터크기 + 바이트순서 + 버전(3자리)
                 그 뒤로 [4글자 이름][크기]… 블록이 이어집니다.
    Blender 는 기본으로 현재 씬의 작은 미리보기를 'TEST' 블록에 넣습니다.
    압축 저장(gzip/zstd)이면 머리말이 안 보이므로 compressed 로 표시합니다.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:2] == b"\x1f\x8b":
                return {"compressed": "gzip", "thumb": None}
            if head[:4] == b"\x28\xb5\x2f\xfd":
                return {"compressed": "zstd", "thumb": None}
            if head[:7] != b"BLENDER":
                return None
            ptr = "64bit" if head[7:8] == b"-" else "32bit"
            ver = head[9:12].decode("ascii", "replace")
            blob = f.read(1 << 20)               # 앞 1MB 안에서 TEST 블록 찾기
            return {"compressed": None, "ptr": ptr, "version": ver,
                    "thumb": b"TEST" in blob}
    except OSError:
        return None


def probe_blend(files, limit=400):
    """[항목 29] .blend 중 썸네일이 들어 있는 비율."""
    ok = thumb = compressed = fail = 0
    versions = Counter()
    for path, size, mtime, ext, stem in files:
        if ext not in (".blend", ".blend1", ".blend2", ".blend3"):
            continue
        if ok + fail + compressed >= limit:
            break
        info = read_blend_header(path)
        if info is None:
            fail += 1
        elif info["compressed"]:
            compressed += 1
        else:
            ok += 1
            versions[info.get("version", "?")] += 1
            if info["thumb"]:
                thumb += 1
    return {"ok": ok, "thumb": thumb, "compressed": compressed,
            "fail": fail, "versions": versions}


# ─────────────────────────────────────────────────────────────
# 6-g. 항목 7·10·24 — 간단한 세기
# ─────────────────────────────────────────────────────────────

def probe_counts(files):
    """[항목 7·10·24] 참조 보유 파일 · 한글 이름 비율 · 비어도비 원본 비율."""
    ref = 0
    ref_bytes = 0
    hangul = 0
    nonadobe = 0
    nonadobe_bytes = 0
    for path, size, mtime, ext, stem in files:
        if ext in REFERENCE_HOLDER_EXTS:
            ref += 1
            ref_bytes += size
        if HANGUL_RE.search(stem):
            hangul += 1
        if ext in NON_ADOBE_SOURCE_EXTS:
            nonadobe += 1
            nonadobe_bytes += size
    n = max(len(files), 1)
    return {"ref": ref, "ref_bytes": ref_bytes,
            "hangul": hangul, "hangul_pct": hangul / n * 100,
            "nonadobe": nonadobe, "nonadobe_bytes": nonadobe_bytes}


# ─────────────────────────────────────────────────────────────
# 6-h. 항목 14·15 — 손으로 채점할 시험지 만들기
#
#   왜 필요한가:
#     "로컬 LLM 이 필요한가"는 코드가 답할 수 없습니다.
#     규칙만으로 몇 %를 맞히는지, LLM 을 붙이면 몇 % 올라가는지를
#     사람이 직접 비교해야 합니다.
#
#   이 파일이 결정하는 것:
#     규칙 85%, LLM 88% 라면 → LLM 을 안 넣는 게 맞습니다.
#     차이가 크면 → 모델 다운로드 부담을 감수할 근거가 생깁니다.
#
#   ⚠ 이 파일에는 실제 파일 이름이 들어갑니다. 공유하거나 커밋하지 마세요.
# ─────────────────────────────────────────────────────────────

def write_manual_testset(files, out_path, n=30):
    """[항목 14·15] 규칙이 내린 분류를 적은 채점표를 만듭니다.

    사용법:
      1. 만들어진 파일을 엽니다
      2. '규칙 답' 이 맞는지 사람이 O/X 로 채점합니다
      3. 같은 목록을 LLM 에게 주고 분류시킨 뒤 같은 방식으로 채점합니다
      4. 두 점수를 비교합니다
    """
    import random
    cands = [f for f in files
             if f[3] in (SOURCE_EXTS | EXPORT_EXTS) and f[1] > 10 * 1024]
    random.seed(0)                            # 매번 같은 표본이 나오게
    sample = random.sample(cands, min(n, len(cands)))

    lines = ["# 규칙 vs LLM 채점표  (항목 14·15)",
             "#",
             "# 규칙 답: 이 프로그램이 파일 이름·확장자·시각만으로 내린 판단",
             "# 채점란에 O 또는 X 를 적으세요. 그다음 같은 목록을 LLM 에게 주고",
             "# 똑같이 채점해 두 점수를 비교하세요.",
             "#",
             "| # | 파일 이름 | 규칙이 본 뼈대 | 규칙 판단 | 채점 |",
             "|---|---|---|---|---|"]
    for i, (path, size, mtime, ext, stem) in enumerate(sample, 1):
        core = normalize_stem(stem) or "(비어 있음)"
        if ext in SOURCE_EXTS:
            guess = "작업 원본"
        elif ext in EXPORT_EXTS:
            guess = "내보낸 결과물"
        else:
            guess = "기타"
        age_y = (time.time() - mtime) / (365 * 86400)
        guess += f" · {age_y:.1f}년 전"
        name = os.path.basename(path).replace("|", "/")
        lines.append(f"| {i} | `{name[:46]}` | `{core[:24]}` | {guess} |  |")

    lines += ["", "## LLM 에게 줄 지시문 (그대로 복사해서 쓰세요)", "",
              "```", "아래 파일 이름들을 보고 각각이 (1) 작업 원본인지 "
              "(2) 내보낸 결과물인지 (3) 기타인지 분류하고,",
              "같은 작업의 다른 버전으로 보이는 것끼리 묶어 주세요. "
              "이유도 한 줄씩 적어 주세요.", "```"]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(sample)




# ─────────────────────────────────────────────────────────────
# 6-i. 공유 모드 — 남의 맥에서 돌린 보고서를 안전하게 받기
#
#   ※ 이건 P0 조사에서만 쓰는 장치입니다.
#     실제 제품(2·3편)은 사용자 본인 맥에서 돌고, 파일이 무엇인지
#     알아야 분류할 수 있으므로 아무것도 가리지 않습니다.
#
#   문제:
#     보고서에 실제 파일 이름이 찍힙니다. 동료가 돌리면
#     클라이언트 이름·학생 이름·계정 이름이 그대로 노출됩니다.
#
#   해법 — 빈도로 가릅니다:
#     이 맥 전체에서 몇 번 나오는 낱말인지를 세고,
#     여러 파일에 반복되는 낱말(업무 어휘)은 남기고
#     한두 파일에만 나오는 낱말(사람 이름·클라이언트명)만 가립니다.
#
#       학생_김OO_포폴_최종.psd  →  학생_[고유]_포폴_최종.psd
#       클라이언트A_시안_수정.ai   →  [고유]_시안_수정.ai
#
#   왜 이렇게 하는가:
#     '학생'·'포폴'·'첨삭' 같은 낱말이 이 사람의 작업 성격을 말해 줍니다.
#     글자 수만 남기면 그 신호가 통째로 사라집니다.
#     가려야 할 것은 누구인지이지, 무슨 일을 하는지가 아닙니다.
#
#   ⚠ 완벽하지 않습니다. 자주 쓰는 클라이언트 이름은 빈도가 높아 남을 수 있습니다.
#     동료에게 무엇이 담기는지 그대로 알리고 동의를 받으세요.
# ─────────────────────────────────────────────────────────────

# 낱말을 가르는 기준 문자
TOKEN_SPLIT_RE = LANG.TOKEN_SPLIT_RE


def tokenize_stem(stem):
    """파일 이름을 낱말로 쪼갭니다. 한글/영문/숫자 경계도 나눕니다."""
    parts = []
    for raw in TOKEN_SPLIT_RE.split(stem):
        if not raw:
            continue
        # 붙어 있는 한글·영문·숫자를 갈라냅니다 (포스터final2 → 포스터/final/2)
        # v2·ver3 같은 버전 표시는 한 낱말로 둡니다 (쪼개면 뜻이 사라집니다)
        parts += [p for p in re.findall(LANG.TOKEN_PATTERN, raw,
                                        re.IGNORECASE) if p]
    return parts


def build_token_vocab(files):
    """이 맥의 파일 이름에 쓰인 낱말 빈도표.

    두 군데에 씁니다:
      1) 공유 모드에서 '가릴 낱말'과 '남길 낱말'을 가르는 기준
      2) [항목 31] 이 사람이 어떤 일을 하는지 읽는 단서
         ('학생'·'첨삭'·'시안'이 많으면 남의 작업을 다루는 사람)
    """
    vocab = Counter()
    for path, size, mtime, ext, stem in files:
        for t in set(tokenize_stem(stem)):        # 한 파일 안 중복은 1회로
            if len(t) >= 2 and not t.isdigit():
                vocab[t.lower()] += 1
    return vocab


# ─────────────────────────────────────────────────────────────
# 6-j. 항목 31 — 남의 작업물이 섞여 있는가
#
#   왜 재는가:
#     미대 졸업생 상당수가 강사·외주를 겸합니다. 그러면 디스크에
#     '내 작업'과 '남의 작업'이 섞입니다. 학생 포폴, 클라이언트 원본,
#     동료가 넘긴 파일 같은 것들입니다.
#
#     사용자는 이걸 압니다. 그래서 정리 앱을 켜면 이렇게 걱정합니다.
#       "내 것도 아닌 파일까지 다 섞어서 옮겨버리면 어떡하지"
#
#     페르소나 진단의 목적이 여기 있습니다. 분석 보고서를 보여주는 게
#     아니라, "당신이 남의 작업물도 다룬다는 걸 알고 있고, 그건 따로
#     두겠습니다" 라고 먼저 말해서 그 걱정을 없애는 것입니다.
#
#   무엇으로 아는가 (증거 사다리):
#     검증됨  파일 안에 다른 사용자의 홈 경로가 박혀 있음 (/Users/남/…)
#     검증됨  XMP 작성자·저장 이력이 나와 다름
#     보조    파일 이름에 '학생'·'첨삭'·'외주' 같은 낱말
#     추측    사람 이름처럼 보이는 낱말
# ─────────────────────────────────────────────────────────────

# 남의 작업을 다룬다는 신호가 되는 낱말
#   강한 단서: 이 낱말이 있으면 거의 확실히 남의 작업입니다
#   약한 단서: 내 작업에도 붙는 말이라 참고만 합니다 ('원본', '시안' 등)
# 남의 작업물 단서 — 낱말은 사전(lang_ko)에 있습니다.
OTHERS_HINTS_STRONG = LANG.OTHERS_STRONG
OTHERS_HINTS_WEAK = LANG.OTHERS_WEAK

# 파일 안에 박혀 있는 다른 사용자의 홈 경로를 찾습니다.
#   psd 의 링크 이미지 경로, indd 의 링크, 프로젝트 파일의 소스 경로 등에
#   `/Users/누구/...` 가 그대로 저장됩니다. 그 '누구'가 나와 다르면
#   그 파일은 다른 사람 컴퓨터에서 만들어진 것입니다.
#
#   ⚠ 계정 이름은 한글일 수 있습니다. 한글은 UTF-8 에서 3바이트라
#     [A-Za-z] 같은 좁은 범위로 잡으면 통째로 놓칩니다.
#     (실제로 24개 중 1개만 잡힌 적이 있습니다)
#     그래서 '슬래시와 제어문자가 아닌 것'으로 넓게 잡고, 뒤에서 걸러냅니다.
USERPATH_RE = re.compile(rb"/Users/([^/\x00-\x1f\\]{1,48})/")


def _plausible_username(raw):
    """찾은 바이트가 진짜 계정 이름 같은지 확인합니다.

    바이너리 쓰레기가 우연히 걸리는 것을 막습니다.
    """
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not (1 <= len(name) <= 32):
        return None
    # 글자·숫자·공백·일부 기호만 허용
    if re.fullmatch(r"[\w가-힣ㄱ-ㅎㅏ-ㅣ .\-]+", name) is None:
        return None
    if name.lower() in ("shared", "guest", "public"):   # 시스템 계정
        return None
    return name


def scan_embedded_users(path, size, head=512 << 10, tail=256 << 10):
    """[항목 31] 파일 안에 박혀 있는 사용자 홈 경로의 주인 이름들.

    ⚠ 속도 주의 — 여기서 한 번 멈춘 적이 있습니다.
      처음엔 파일당 6MB(앞 4MB + 뒤 2MB)를 읽었습니다. 800개면 4.8GB 를
      디스크에서 무작위로 읽는 셈이라, 10분이 지나도 화면이 안 움직여서
      멈춘 것처럼 보였습니다.

      원인은 정규식이 아니라 디스크 읽기였습니다.
      (처음엔 정규식 탓인 줄 알고 b"/Users/" 사전 검사를 넣었는데,
       재보니 파이썬 정규식이 패턴 앞의 고정 문자열을 이미 빠르게 훑고
       있어서 효과가 없었습니다. 오히려 같은 일을 두 번 해서 느렸습니다.
       측정 없이 원인을 짐작하면 이런 일이 생깁니다.)

      고친 것:
        읽는 양   6 MB   → 768 KB   (8배)
        파일 수   800개  → 250개    (3.2배)
        ────────────────────────────────
        총 읽기   4.8 GB → 192 MB   (25배)

      링크 경로는 파일 앞뒤에 몰려 있어 768KB 로도 대부분 잡힙니다.
    """
    names = set()

    def collect(blob):
        for m in USERPATH_RE.finditer(blob):
            n = _plausible_username(m.group(1))
            if n:
                names.add(n)

    try:
        with open(path, "rb") as f:
            collect(f.read(head))
            if size > head + tail:
                f.seek(size - tail)
                collect(f.read(tail))
    except OSError:
        return set()
    return names


def probe_ownership(files, vocab, limit=250, time_budget=45):
    """[항목 31] 남의 작업물이 얼마나 섞여 있는가.

    이 숫자가 결정하는 것:
      비중이 크면 → 3편에 '소유 구분' 단계를 검토합니다.
      단, 이 사람의 직업 특성일 수 있으므로 결론은 내지 않습니다.

    time_budget: 이 초를 넘기면 검사한 만큼만 가지고 끝냅니다.
                 P0 는 정밀 측정이 아니라 방향 판단이 목적이므로,
                 표본 250개로도 비율은 충분히 나옵니다.
    """
    me = os.path.basename(os.path.expanduser("~"))

    # (1) 파일 안에 박힌 다른 사용자 경로 — 가장 강한 증거
    targets = [f for f in files if f[3] in REFERENCE_HOLDER_EXTS]
    targets.sort(key=lambda f: -f[1])
    targets = targets[:limit]

    checked = 0
    with_foreign = 0
    foreign_names = Counter()
    foreign_bytes = 0
    started = time.time()
    stopped_early = False

    for i, (path, size, mtime, ext, stem) in enumerate(targets):
        if time.time() - started > time_budget:
            stopped_early = True
            break
        if i and i % 25 == 0:                      # 멈춘 게 아니라는 표시
            print(f"    …{i}/{len(targets)}", flush=True)
        checked += 1
        found = scan_embedded_users(path, size)
        others = {n for n in found if n != me}
        if others:
            with_foreign += 1
            foreign_bytes += size
            for n in others:
                foreign_names[n] += 1

    # (2) 파일 이름에 담긴 낱말 — 보조 증거
    #     강한 단서와 약한 단서를 따로 셉니다. '원본'·'시안'은 내 작업에도
    #     붙는 말이라 같이 세면 남의 작업 비중이 부풀려집니다.
    strong_hits = Counter()
    weak_hits = Counter()
    strong_files = weak_files = 0
    strong_bytes = 0
    for path, size, mtime, ext, stem in files:
        toks = {t.lower() for t in tokenize_stem(stem)}
        st = {lb for k, lb in OTHERS_HINTS_STRONG if k in toks}
        wk = {lb for k, lb in OTHERS_HINTS_WEAK if k in toks}
        if st:
            strong_files += 1
            strong_bytes += size
            for lb in st:
                strong_hits[lb] += 1
        elif wk:
            weak_files += 1
            for lb in wk:
                weak_hits[lb] += 1

    return {"me": me, "checked": checked, "with_foreign": with_foreign,
            "foreign_bytes": foreign_bytes,
            "foreign_count": len(foreign_names),
            "foreign_names_sample": [n for n, _ in foreign_names.most_common(5)],
            "stopped_early": stopped_early,
            "strong_files": strong_files, "strong_bytes": strong_bytes,
            "strong": strong_hits,
            "weak_files": weak_files, "weak": weak_hits,
            "total_files": len(files)}







# ─────────────────────────────────────────────────────────────
# 6-k. 항목 32 — 폴더 프로파일 (특수상황 감지의 일반 원리)
#
#   왜 이렇게 하는가:
#     특수상황은 종류가 무한합니다. 유학원 강사, 사진 스튜디오, 번역가,
#     웨딩 촬영, 인쇄소… 감지기를 하나씩 만들면 절대 못 따라갑니다.
#     (실제로 '학생 이름 감지기'를 만들었다가 그게 한 직업에만 통한다는
#      걸 알고 되돌린 적이 있습니다)
#
#   그래서 기준선을 '이 사람의 다른 폴더'로 삼습니다.
#     세상의 모든 직업을 몰라도, 이 맥 안에서 튀는 폴더는 찾을 수 있습니다.
#     그 폴더가 왜 튀는지는 LLM 이 문장으로 설명하면 됩니다.
#
#   여기서 뽑는 신호는 전부 파일 이름의 '뜻'이 아니라 '모양'입니다.
#     → 그래서 LLM 에 보낼 때 파일 이름이 필요 없습니다.
#       (지난 결정 "이름 붙은 데이터는 밖으로 안 나간다"와 자동으로 맞습니다)
#
#   이 숫자가 결정하는 것:
#     튀는 폴더가 실제로 의미 있게 잡히면 → 2편 페르소나 화면의 근거가 됩니다.
#     아무것도 안 튀거나 엉뚱한 게 튀면 → 다른 신호를 찾아야 합니다.
# ─────────────────────────────────────────────────────────────

def _median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _mad(xs, med):
    """중앙값 절대편차. 평균·표준편차보다 이상치에 덜 흔들립니다."""
    if not xs:
        return 0.0
    return _median([abs(x - med) for x in xs])


def profile_folders(files, btimes, min_files=5, max_folders=4000):
    """[항목 32] 폴더마다 '모양'을 재고, 이 사람의 다른 폴더와 비교합니다.

    재는 것 (전부 이름의 뜻과 무관):
      untouched   생성일 = 수정일 인 비율  → 받아만 놓고 안 연 파일
      namelen     이름 길이 평균           → 짧고 균일하면 목록·명단
      namevar     이름 길이의 흔들림       → 작으면 규칙적
      timespan    수정일이 퍼진 기간(일)   → 좁으면 한 시기에 몰린 묶음
      extmix      최빈 확장자 비율         → 1에 가까우면 한 종류만
      pairless    원본↔내보낸것 쌍이 없는 비율 → 내가 만든 게 아닐 수 있음
      exclusive   이 폴더에서만 쓰는 낱말 비율 → 외부에서 들어온 묶음
      digitstart  숫자로 시작하는 이름 비율  → 날짜 규칙을 쓰던 시기
    """
    # 낱말이 어느 폴더들에 나오는지 (exclusive 계산용)
    token_dirs = defaultdict(set)
    by_dir = defaultdict(list)
    for f in files:
        d = os.path.dirname(f[0])
        by_dir[d].append(f)
        for t in set(tokenize_stem(f[4])):
            if len(t) >= 2:
                token_dirs[t.lower()].add(d)

    profiles = []
    for d, group in list(by_dir.items())[:max_folders]:
        if len(group) < min_files:
            continue

        stems = [f[4] for f in group]
        lens = [len(s) for s in stems]
        mtimes = [f[2] for f in group]

        # 받아만 놓고 안 연 파일 — 생성 시각과 수정 시각이 거의 같은 것
        #   ※ st_birthtime 은 macOS 에만 있습니다. 없으면 이 신호는 건너뜁니다.
        untouched = None
        pairs = [(btimes.get(f[0]), f[2]) for f in group]
        known = [(b, m) for b, m in pairs if b]
        if known:
            untouched = sum(1 for b, m in known if abs(m - b) < 60) / len(known)

        # 이름 뼈대가 같은데 확장자만 다른 짝 (원본 ↔ 내보낸 것)
        keyed = defaultdict(set)
        for f in group:
            keyed[normalize_stem(f[4])].add(f[3])
        paired = sum(1 for exts in keyed.values()
                     if (exts & SOURCE_EXTS) and (exts & EXPORT_EXTS))
        pairless = 1 - (paired / max(len(keyed), 1))

        # 이 폴더에서만 쓰이는 낱말
        toks = [t.lower() for s in stems for t in tokenize_stem(s) if len(t) >= 2]
        excl = (sum(1 for t in toks if len(token_dirs[t]) <= 1) / len(toks)
                if toks else 0)

        extc = Counter(f[3] for f in group)
        span = (max(mtimes) - min(mtimes)) / 86400 if len(mtimes) > 1 else 0
        mean_len = sum(lens) / len(lens)

        profiles.append({
            "dir": d,
            "n": len(group),
            "bytes": sum(f[1] for f in group),
            "untouched": untouched,
            "namelen": mean_len,
            "namevar": (sum(abs(x - mean_len) for x in lens) / len(lens)),
            "timespan": span,
            "extmix": extc.most_common(1)[0][1] / len(group),
            "pairless": pairless,
            "exclusive": excl,
            "digitstart": sum(1 for s in stems if s[:1].isdigit()) / len(stems),
            "topext": extc.most_common(1)[0][0],
        })
    return profiles


# 신호 정의
#   key       프로파일에서 읽을 값
#   dir       어느 쪽으로 튀어야 이상한가 (high / low)
#   log       값이 자릿수 단위로 벌어지는가 (0일 ~ 1000일). 로그로 눌러 비교
#   floor     최소 편차. 이보다 촘촘하면 비교하지 않습니다.
#             ⚠ 없으면 사고가 납니다 — 대부분의 폴더가 900~1000일로 촘촘하면
#               825일짜리가 '한 시기에 몰려 있음'으로 잡힙니다. 실제로 났던 오탐입니다.
#   unit      보고서 표시 단위
#   label     사람이 읽을 설명
OUTLIER_SIGNALS = [
    {"key": "untouched",  "dir": "high", "log": False, "floor": 0.08,
     "unit": "%",  "label": "받아만 놓고 열지 않은 파일이 많음"},
    {"key": "namelen",    "dir": "low",  "log": False, "floor": 1.5,
     "unit": "글자", "label": "이름이 유난히 짧음 (목록·명단일 수 있음)"},
    {"key": "namevar",    "dir": "low",  "log": True,  "floor": 0.25,
     "unit": "±글자", "label": "이름 길이가 유난히 고름 (규칙적으로 붙임)"},
    {"key": "timespan",   "dir": "low",  "log": True,  "floor": 0.7,
     "unit": "일", "label": "한 시기에 몰려 있음 (한 프로젝트·한 학기)"},
    {"key": "extmix",     "dir": "high", "log": False, "floor": 0.10,
     "unit": "%",  "label": "한 종류 파일만 있음 (수집물일 수 있음)"},
    {"key": "pairless",   "dir": "high", "log": False, "floor": 0.15,
     "unit": "%",  "label": "내보낸 결과물이 거의 없음 (내가 만든 게 아닐 수 있음)"},
    {"key": "exclusive",  "dir": "high", "log": False, "floor": 0.10,
     "unit": "%",  "label": "이 폴더에서만 쓰는 말이 많음 (밖에서 들어온 묶음)"},
    {"key": "digitstart", "dir": "high", "log": False, "floor": 0.15,
     "unit": "%",  "label": "숫자로 시작하는 이름이 많음 (날짜 규칙을 쓰던 시기)"},
]

# 이보다 폴더가 적으면 '보통'이 뭔지 알 수 없어 비교가 무의미합니다.
MIN_FOLDERS_FOR_COMPARE = 8


def find_unusual_folders(profiles, top=6, threshold=2.0):
    """[항목 32] 이 사람의 다른 폴더와 비교해 튀는 폴더를 고릅니다.

    중앙값에서 얼마나 떨어졌는지로 봅니다 (MAD 기준).
    평균을 쓰면 튀는 폴더 자신이 평균을 끌어당겨 안 잡힙니다.

    ⚠ 폴더가 적으면 못 씁니다. '보통'의 기준이 없기 때문입니다.
    ⚠ 여기서는 '왜 튀는지' 를 말하지 않습니다. 신호 이름만 답니다.
      해석은 LLM 이 하거나 사용자가 확인합니다. 프로그램이 직업을
      단정하면 틀렸을 때 신뢰를 잃습니다.
    """
    if len(profiles) < MIN_FOLDERS_FOR_COMPARE:
        return []

    def val(p, sig):
        v = p[sig["key"]]
        if v is None:
            return None
        return math.log1p(v) if sig["log"] else v

    stats = {}
    for sig in OUTLIER_SIGNALS:
        vals = [val(p, sig) for p in profiles]
        vals = [v for v in vals if v is not None]
        if len(vals) < MIN_FOLDERS_FOR_COMPARE // 2:
            continue
        med = _median(vals)
        mad = max(_mad(vals, med), sig["floor"])   # 바닥값으로 오탐 차단
        stats[sig["key"]] = (med, mad)

    scored = []
    for p in profiles:
        hits = []
        score = 0.0
        for sig in OUTLIER_SIGNALS:
            if sig["key"] not in stats:
                continue
            v = val(p, sig)
            if v is None:
                continue
            med, mad = stats[sig["key"]]
            z = (v - med) / mad
            if (sig["dir"] == "high" and z > threshold) or \
               (sig["dir"] == "low" and z < -threshold):
                hits.append((sig, p[sig["key"]], z))
                score += abs(z)
        if hits:
            scored.append({"p": p, "hits": hits, "score": score})

    scored.sort(key=lambda s: -s["score"])
    return scored[:top]


def format_signal(sig, value):
    """신호 값을 단위에 맞춰 보여 줍니다."""
    if sig["unit"] == "%":
        return f"{value * 100:.0f}%"
    if sig["unit"] == "일":
        return f"{value:.0f}일" if value >= 1 else "하루 안"
    return f"{value:.1f}{sig['unit']}"




# ─────────────────────────────────────────────────────────────
# 6-l. 등급 — "뭘 남기지?" 를 판단 난이도별로 나눕니다
#
#   지금까지 완전 중복과 버전 가족을 따로 봤는데,
#   사용자 입장에서는 같은 질문입니다 — "이 중에 뭘 남기지?"
#
#   그런데 판단의 난이도가 다릅니다.
#
#     1급  완전히 같음     해시 일치        "하나만 남기세요"     판단 불필요
#     2급  거의 같음       지문이 가까움     "미세한 차이가 있어요"  설명 필요
#     3급  같은 작업의 버전  계보 일치        "이게 채택본 같아요"   기억 필요
#     4급  아마 관련       이름만 비슷       "확인해 주세요"       사용자 판단
#
#   위로 갈수록 버튼 하나로 끝나고, 아래로 갈수록 화면이 필요합니다.
#   1편의 ①~⑤ 구간과 같은 구조입니다.
#
#   2급이 핵심입니다 — "포토샵으로 미세하게 보정한 차이" 를 알아보고
#   어느 쪽을 남길지 조언하는 것. 그러려면 무엇이 다른지 말할 수 있어야 합니다.
#
#   이 측정이 결정하는 것:
#     각 등급이 몇 개·몇 GB 인가 → 3편 화면을 어떻게 나눌지
#     2급에서 실제로 차이를 설명할 수 있는가 → 조언 기능이 성립하는가
# ─────────────────────────────────────────────────────────────

def measure_signal_validity(psd_result, lineage):
    """이 사람에게 어떤 신호가 유효한지 먼저 잽니다.

    ⚠ 신호를 모든 사람에게 똑같이 적용하면 안 됩니다.

      색공간(RGB→CMYK)이 대표적입니다. 인쇄를 넘기는 사람에게는
      "이게 최종본" 이라는 강한 신호지만, **인쇄를 안 하는 사람에게는
      아무 뜻이 없습니다.** 웹·영상 작업자는 최종본도 RGB 입니다.

      실측에서 이 맥은 PSD 239개 중 CMYK 가 2개(1%)뿐이었습니다.
      이런 사람에게 "CMYK 니까 이게 최종본" 이라고 하면 틀립니다.
      오히려 그 2개가 예외적인 파일일 수 있습니다.

    항목 32(튀는 폴더)와 같은 원리입니다 —
    기준선을 이 사람 자신으로 삼습니다.
    """
    v = {}

    # 색공간 — 인쇄 작업을 하는 사람인가
    modes = (psd_result or {}).get("modes") or Counter()
    total = sum(modes.values())
    cmyk = modes.get("CMYK", 0)
    ratio = cmyk / total if total else 0
    v["colorspace"] = {
        "usable": total >= 20 and 0.05 <= ratio <= 0.95,
        "ratio": ratio,
        "why": (f"PSD 중 CMYK 가 {ratio*100:.0f}% — "
                + ("인쇄 작업을 하시는군요. 색공간이 유효한 신호입니다"
                   if total >= 20 and 0.05 <= ratio <= 0.95
                   else "인쇄를 거의 안 하시는 것 같아 색공간은 신호로 안 씁니다")),
    }

    # 저장 횟수 — 편차가 있어야 신호가 됩니다
    avg = (lineage or {}).get("save_avg") or 0
    mx = (lineage or {}).get("save_max") or 0
    v["save_count"] = {
        "usable": mx >= 5 and mx > avg * 1.5,
        "why": (f"저장 횟수 평균 {avg:.1f}회 · 최대 {mx}회 — "
                + ("편차가 있어 신호로 쓸 수 있습니다"
                   if mx >= 5 and mx > avg * 1.5
                   else "편차가 작아 신호가 약합니다")),
    }

    # 만든 프로그램 — 여러 도구를 쓰는 사람인가
    tools = (lineage or {}).get("tools") or Counter()
    v["tool"] = {
        "usable": len(tools) >= 2,
        "why": (f"쓰신 프로그램 {len(tools)}종 — "
                + ("도구가 바뀐 흔적을 신호로 쓸 수 있습니다"
                   if len(tools) >= 2 else "한 가지만 쓰셔서 신호가 안 됩니다")),
    }
    return v


def describe_difference(a, b, validity=None):
    """두 파일의 메타데이터를 비교해 '무엇이 다른가' 를 문장으로 만듭니다.

    a, b 는 {size, mtime, xmp, psd} 형태.
    validity 는 measure_signal_validity() 의 결과 — 이 사람에게
    어떤 신호가 유효한지. 없으면 전부 유효하다고 봅니다.

    ⚠ 숫자를 나열하면 안 됩니다.
      "저장 12회 · dHash 4 · CMYK" 는 아무것도 안 떠오르게 합니다.
      "포토샵에서 12번 저장하며 손보셨고 인쇄용으로 바꾸셨네요" 여야 합니다.
    """
    v = validity or {}

    def ok(key):
        return v.get(key, {}).get("usable", True)

    reasons = []
    keep = None          # "b" 또는 "a"

    # ⚠ 작업 원본(psd)과 내보낸 결과물(jpg)은 '버전' 이 아닙니다.
    #   둘 다 필요한 짝인데 "하나를 남기세요" 라고 하면 안 됩니다.
    #   실측에서 'HWAR4887.jpg ↔ HWAR4887.psd → 뒤쪽을 남기세요' 라는
    #   잘못된 조언이 나왔습니다.
    ea = os.path.splitext(a.get("path", ""))[1].lower()
    eb = os.path.splitext(b.get("path", ""))[1].lower()
    if (ea in SOURCE_EXTS and eb in EXPORT_EXTS) or \
       (eb in SOURCE_EXTS and ea in EXPORT_EXTS):
        return ("작업 원본과 내보낸 결과물입니다. 버전이 아니라 짝이라 "
                "둘 다 두시는 게 맞습니다", None)

    ax, bx = a.get("xmp") or {}, b.get("xmp") or {}

    # 만든 프로그램이 다름 — 가장 알아보기 쉬운 차이
    at, bt = (ax.get("tool") or ""), (bx.get("tool") or "")
    if ok("tool") and at and bt and at != bt:
        reasons.append(f"만든 프로그램이 다릅니다 ({at[:22]} / {bt[:22]})")
        # 사진 도구 → 편집 도구 면 뒤쪽이 손본 것
        edit_words = LANG.EDIT_TOOL_WORDS
        if any(w in bt.lower() for w in edit_words) and \
           not any(w in at.lower() for w in edit_words):
            keep = "b"
            reasons.append("뒤쪽을 편집 도구에서 손보신 것 같습니다")

    # 저장 횟수 — 얼마나 붙들고 있었나
    asv, bsv = ax.get("save_count") or 0, bx.get("save_count") or 0
    if ok("save_count") and asv and bsv and abs(asv - bsv) >= 3:
        more = "뒤" if bsv > asv else "앞"
        reasons.append(f"{more}쪽을 훨씬 많이 저장하셨습니다 ({asv}회 / {bsv}회)")
        keep = keep or ("b" if bsv > asv else "a")

    # 색공간 — 인쇄 준비 신호.
    #   ⚠ 인쇄를 안 하는 사람에게는 쓰지 않습니다 (validity 참조)
    ap_, bp_ = (a.get("psd") or {}), (b.get("psd") or {})
    am, bm = ap_.get("mode"), bp_.get("mode")
    if ok("colorspace") and am != bm and "CMYK" in (am, bm):
        side = "뒤" if bm == "CMYK" else "앞"
        reasons.append(f"{side}쪽은 인쇄용(CMYK)으로 바꾸셨습니다")
        keep = "b" if bm == "CMYK" else "a"

    # 크기 차이
    if a["size"] and b["size"]:
        diff = abs(a["size"] - b["size"]) / max(a["size"], b["size"])
        if diff > 0.05:
            bigger = "뒤" if b["size"] > a["size"] else "앞"
            reasons.append(f"{bigger}쪽이 {diff*100:.0f}% 큽니다 "
                           "(레이어나 보정이 더 들어갔을 수 있습니다)")

    if not reasons:
        return ("차이를 설명할 단서가 없습니다", None)
    return (" · ".join(reasons), keep)


def grade_families(files, dups, name_fams, lineage, phash_fams):
    """[등급] 각 등급이 몇 개·몇 GB 인지 셉니다.

    등급이 겹치면 위쪽(판단이 쉬운 쪽)으로 넣습니다.
    같은 파일을 두 번 세지 않기 위해서입니다.
    """
    counted = set()
    out = {}

    def add(key, groups, label):
        n = b = 0
        fams = 0
        for g in groups:
            paths = [p for p in g if p not in counted]
            if len(paths) < 2:
                continue
            fams += 1
            sizes = [sz for p, sz in
                     ((p, size_of.get(p, 0)) for p in paths)]
            n += len(paths) - 1
            b += sum(sorted(sizes)[:-1])      # 가장 큰 것 하나는 남김
            counted.update(paths)
        out[key] = {"label": label, "families": fams, "extra_files": n,
                    "bytes": b}

    size_of = {f[0]: f[1] for f in files}

    # 1급 — 완전히 같음
    g1 = [paths for _, paths in dups.get("samples", [])]
    add("1급", g1, "완전히 같음 — 하나만 남기면 됩니다")

    # 2급 — 겉모습이 거의 같음
    g2 = [[m[0] for m in fam] for fam in (phash_fams or [])]
    add("2급", g2, "거의 같은데 조금 다름 — 무엇이 다른지 설명이 필요합니다")

    # 3급 — 계보가 같음
    g3 = []
    if lineage:
        for _, members in (lineage.get("lineage_families") or {}).items():
            g3.append([m[0] for m in members])
    add("3급", g3, "같은 작업의 다른 버전 — 어느 게 채택본인지 기억이 필요합니다")

    # 4급 — 이름만 비슷
    g4 = [[m[0] for m in members] for _, members in name_fams]
    add("4급", g4, "이름이 비슷함 — 사용자가 확인해야 합니다")

    return out


def probe_phash_distance(phash_fams):
    """[등급 2급] 겉모습 지문이 얼마나 가까운지 분포를 봅니다.

    0    픽셀까지 같음      → 사실상 1급
    1~5  거의 같음          → 미세한 보정
    6~10 비슷함             → 다른 버전
    11+  다름               → 우연

    이 분포가 갈리면 '완전히 같음' 과 '조금 다름' 을 자동으로 나눌 수 있습니다.
    안 갈리면 지문만으로는 등급을 못 나눈다는 뜻입니다.
    """
    buckets = Counter()
    for fam in (phash_fams or []):
        base = fam[0][2]
        for m in fam[1:]:
            d = hamming(base, m[2])
            if d == 0:
                buckets["0 (픽셀까지 같음)"] += 1
            elif d <= 5:
                buckets["1~5 (미세한 차이)"] += 1
            elif d <= 10:
                buckets["6~10 (다른 버전)"] += 1
            else:
                buckets["11+ (아마 다름)"] += 1
    return buckets


def build_advice_examples(files, lineage, cache, validity=None, limit=8):
    """[조언] 같은 가족 안에서 '무엇이 다른가' 문장을 실제로 만들어 봅니다.

    이게 3편의 핵심 기능입니다 —
      "이건 완전히 같아요. 하나만 남기세요."
      "이건 포토샵으로 미세하게 보정한 차이가 있네요. 이걸 남기시죠."

    여기서 문장이 안 나오면 그 기능은 성립하지 않습니다.
    P0 의 목적은 그게 되는지 확인하는 것입니다.
    """
    if not lineage:
        return []
    fams = lineage.get("lineage_families") or {}
    meta = {f[0]: (f[1], f[2]) for f in files}
    psd_cache = {}

    out = []
    for orig, members in list(fams.items())[:limit * 3]:
        if len(members) < 2:
            continue
        # 저장 횟수가 가장 적은 것과 가장 많은 것을 비교합니다
        ms = sorted(members, key=lambda m: (m[3] or {}).get("save_count") or 0)
        a_raw, b_raw = ms[0], ms[-1]

        def pack(m):
            path = m[0]
            size, mtime = meta.get(path, (m[1], 0))
            if path not in psd_cache:
                psd_cache[path] = (read_psd_header(path)
                                   if path.lower().endswith((".psd", ".psb"))
                                   else None)
            return {"path": path, "size": size, "mtime": mtime,
                    "xmp": m[3], "psd": psd_cache[path]}

        msg, keep = describe_difference(pack(a_raw), pack(b_raw), validity)
        if "짝이라" in msg:          # 원본↔결과물은 조언 대상이 아닙니다
            continue
        if keep or "없습니다" not in msg:
            out.append((os.path.basename(a_raw[0]) + " ↔ "
                        + os.path.basename(b_raw[0]), msg, keep))
        if len(out) >= limit:
            break
    return out


def probe_dup_locations(dups, root):
    """[중복 위치] 중복이 어디에 있는지 봅니다.

    17.56GB 가 나왔는데 그게 진짜 작업물 중복인지,
    Unity 캐시·에셋 사본 같은 '다시 생기는 것' 인지 구분해야 합니다.
    후자면 그건 중복이 아니라 1편의 ②구간(캐시)입니다.
    """
    hint_dirs = Counter()
    hint_kind = Counter()
    KINDS = LANG.DUP_ORIGIN_HINTS
    for size, paths in dups.get("samples", []):
        for p in paths:
            d = os.path.dirname(p)
            try:
                hint_dirs[os.path.relpath(d, root)[:50]] += 1
            except ValueError:
                continue
            for key, label in KINDS:
                if key.lower() in p.lower():
                    hint_kind[label] += 1
                    break
            else:
                hint_kind["작업물로 보임"] += 1
    return {"dirs": hint_dirs, "kinds": hint_kind}




# ─────────────────────────────────────────────────────────────
# 7. 환경
# ─────────────────────────────────────────────────────────────

def sh(cmd):
    """셸 명령을 돌려 표준출력만 돌려줍니다. 실패하면 빈 문자열."""
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def environment():
    """[항목 2·11·12·13] 이 맥이 어떤 환경인가.

    - Intel/Apple Silicon: 로컬 LLM(MLX·오사우르스)을 쓸 수 있는지 가름
    - macOS 26 이상: Apple Foundation Models 를 공짜로 쓸 수 있는지
    - mdfind 응답 시간: 2편의 스캔 화면을 2초로 짤지 3분으로 짤지
    - 1337 포트: 오사우르스가 켜져 있는지
    """
    env = {
        "os": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }
    if platform.system() == "Darwin":
        env["macos"] = sh("sw_vers -productVersion")
        env["chip"] = sh("sysctl -n machdep.cpu.brand_string")
        env["ram_gb"] = round(int(sh("sysctl -n hw.memsize") or 0) / 2**30, 1)
        env["spotlight"] = "켜짐" if "Indexing enabled" in sh("mdutil -s / 2>/dev/null") else "확인 필요"
        # mdfind 속도
        t0 = time.time()
        sh("mdfind -onlyin ~ 'kMDItemContentTypeTree == \"public.image\"' -count")
        env["mdfind_sec"] = round(time.time() - t0, 2)
        env["osaurus"] = "응답" if sh("curl -s -m 2 http://127.0.0.1:1337/v1/models") else "없음"
    return env


# ─────────────────────────────────────────────────────────────
# 8. 보고서
# ─────────────────────────────────────────────────────────────

def gb(n):
    """바이트를 GB 문자열로."""
    return f"{n / 2**30:,.2f} GB"


def row(label, value, width=34):
    """보고서 한 줄을 라벨/값 두 칸으로 정렬합니다."""
    return f"  {label:<{width}} {value}"


def build_report(env, sv, analysis, elapsed, share=False):
    """측정값을 사람이 읽을 보고서로 만듭니다. 판정 문장까지 붙입니다.

    보고서의 목적은 숫자 나열이 아니라 '무엇을 먼저 만들지' 결정입니다.
    그래서 마지막 '판정' 절에서 각 숫자가 어떤 결론을 가리키는지 문장으로 씁니다.
    """
    vocab = analysis.get("vocab")
    L = []
    A = L.append
    A("=" * 68)
    A(f"  P0 실측 보고서  ({VERSION})")
    A(f"  대상: {'(가려짐)' if share else sv.root}")
    if share:
        A("  공유용 — 파일 이름·경로가 나오는 절은 담지 않았습니다.")
    A(f"  소요: {elapsed:.1f}초" + ("   ※ 파일 수 상한에 걸려 일부만 봤습니다" if sv.hit_limit else ""))
    A("=" * 68)

    # ── 환경
    A("")
    A("── 환경 ─────────────────────────────────────────────")
    A(row("운영체제", f"{env.get('os')} {env.get('macos','')}"))
    A(row("칩", f"{env.get('chip', env.get('arch'))}"))
    A(row("메모리", f"{env.get('ram_gb','?')} GB"))
    A(row("Spotlight", env.get("spotlight", "-")))
    A(row("mdfind 응답", f"{env.get('mdfind_sec','-')}초"))
    A(row("오사우르스(1337포트)", env.get("osaurus", "-")))

    silicon = "arm" in str(env.get("arch", "")).lower()
    A("")
    if env.get("os") == "Darwin" and not silicon:
        A("  ⚠ Intel 맥입니다. 로컬 LLM(오사우르스/MLX)은 못 씁니다.")
        A("    → 규칙 엔진 중심 설계가 필수입니다.")

    # ── 규모
    A("")
    A("── 규모 ─────────────────────────────────────────────")
    A(row("훑은 파일", f"{len(sv.files):,} 개"))
    A(row("총 용량", gb(sv.total_bytes)))
    A(row("건너뛴 클라우드", f"{sv.cloud_files:,} 개 · {gb(sv.cloud_bytes)}"))
    A(row("건너뛴 앱 라이브러리", f"{sv.bundle_count:,} 개 · {gb(sv.bundle_bytes)}"))
    touchable = len(sv.files)
    total_seen = touchable + sv.cloud_files + sv.bundle_count
    pct = (touchable / total_seen * 100) if total_seen else 0
    A(row("★ 실제로 손댈 수 있는 비율", f"{pct:.1f}%"))

    # ── 실패 (숨기지 않습니다)
    A("")
    A("── 못 읽은 것 ───────────────────────────────────────")
    if not sv.errors:
        A("  없음")
    else:
        for k, v in sv.errors.most_common():
            A(row(k, f"{v:,} 곳"))
        for kind, p in sv.error_samples[:5]:
            if not share:
                A(f"      · {p}")

    # ── 2편 신호
    A("")
    A("── 2편 신호 (전체 재편) ─────────────────────────────")
    ab, asz = analysis["age"]
    for k in ["1년 이내", "1~2년", "2~3년", "3~5년", "5년 이상"]:
        A(row(f"  {k}", f"{ab.get(k,0):>8,} 개 · {gb(asz.get(k,0))}"))
    A("")
    nq = analysis["names"]
    tot = sum(nq.values()) or 1
    for k, v in nq.most_common():
        A(row(f"  이름: {k}", f"{v:>8,} 개 ({v/tot*100:4.1f}%)"))

    # ── 3편 신호
    A("")
    A("── 3편 신호 (버전·중복 정리) ───────────────────────")
    fams = analysis["families"]
    fam_files = sum(len(v) for _, v in fams)
    fam_bytes = sum(sum(f[1] for f in v) for _, v in fams)
    fam_excess = sum(sum(f[1] for f in v) - max(f[1] for f in v) for _, v in fams)
    A(row("버전 가족 수", f"{len(fams):,} 개"))
    A(row("가족에 속한 파일", f"{fam_files:,} 개 · {gb(fam_bytes)}"))
    A(row("★ 가장 큰 것만 남기면", f"{gb(fam_excess)} 회수 가능"))

    dp = analysis["dups"]
    A(row("완전 중복 파일", f"{dp['files']:,} 개 · {gb(dp['bytes'])}"))
    A("    ※ 크기·앞 64KB·뒤 64KB 가 같다는 뜻입니다. 중간까지 대조하지는")
    A("      않았으므로 '거의 확실히 같음' 입니다. 실제로 지울 때는")
    A("      전체를 읽어 확인하는 단계가 따로 필요합니다.")
    if dp["groups_total"]:
        scope = (f"{dp['groups_checked']:,}/{dp['groups_total']:,} 묶음"
                 f" · {dp['min_size']//2**20}MB 이상만")
        if dp["stopped_early"]:
            scope += "  (시간 제한으로 중단 — 실제는 이보다 많습니다)"
        A(row("  검사 범위", scope))
        if dp["stage1"]:
            saved = 1 - dp["stage2"] / max(dp["stage1"], 1)
            A(row("  1차(앞 16KB) / 2차(뒤까지)",
                  f"{dp['stage1']:,}회 / {dp['stage2']:,}회"
                  f"  → 비싼 읽기 {saved*100:.0f}% 절약"))
        if dp.get("unreadable"):
            A(row("  ⚠ 못 읽은 파일",
                  f"{dp['unreadable']:,} 개 — 중복인지 아닌지 모릅니다"))
            for p in dp["unreadable_samples"][:3]:
                A(f"      {p}")

    cache_total = sum(v[1] for v in sv.cache_hits.values())
    A(row("★ 캐시·렌더 (다시 생김)", gb(cache_total)))
    for label, (n, b) in sorted(sv.cache_hits.items(), key=lambda x: -x[1][1]):
        if b > 0:
            A(row(f"    {label}", f"{n:,} 개 · {gb(b)}"))

    ws, wb = analysis["deriv"]
    A("")
    A(row("작업 원본이 있는 이름", f"{ws:,} 개"))
    A(row("  옆에 내보낸 결과물도 있음", f"{wb:,} 개 ({wb/max(ws,1)*100:.0f}%) ← 보조 증거"))

    # ── 계보 (파일 이름을 못 믿을 때)
    lin = analysis.get("lineage")
    if lin:
        A("")
        A("── 계보 (XMP) ──────────────────────────────────────")
        chk = max(lin["checked"], 1)
        A(row("XMP 검사한 파일",
              f"{lin['checked']:,} 개"
              + ("  (시간 제한으로 중단)" if lin.get("stopped_early") else "")))
        A(row("★ XMP 있음", f"{lin['found']:,} 개 ({lin['found']/chk*100:.0f}%)"))
        A(row("  원본 ID 있음", f"{lin['with_orig']:,} 개"))
        A(row("  파생 관계 있음", f"{lin['with_derived']:,} 개"))
        A(row("★ 평균 저장 횟수", f"{lin['save_avg']:.1f} 회 (최대 {lin['save_max']})"))
        if lin["tools"]:
            A("  만든 프로그램:")
            for t, n in lin["tools"].most_common(5):
                A(f"      {t:<44} {n:,}")

        cmp_ = analysis.get("family_cmp")
        if cmp_:
            A("")
            A(row("이름으로 묶은 가족", f"{cmp_['name_families']:,} 개"))
            A(row("계보로 묶은 가족", f"{cmp_['lineage_families']:,} 개"))
            A(row("★★ 계보만 찾아낸 파일", f"{cmp_['lineage_only']:,} 개  ← 이름이 놓친 것"))
            A(row("   이름만 찾아낸 파일", f"{cmp_['name_only']:,} 개"))
            A(row("   둘 다 찾아낸 파일", f"{cmp_['both']:,} 개"))

    # ── PSD 안쪽
    psd = analysis.get("psd")
    if psd and (psd["ok"] or psd["fail"]):
        A("")
        A("── PSD 안쪽 ────────────────────────────────────────")
        A(row("헤더 읽기 성공", f"{psd['ok']:,} / {psd['ok']+psd['fail']:,}"))
        if psd["modes"]:
            tot = sum(psd["modes"].values())
            for m, n in psd["modes"].most_common():
                mark = "  ← 인쇄 준비 신호" if m == "CMYK" else ""
                A(row(f"  색공간: {m}", f"{n:,} ({n/tot*100:.0f}%){mark}"))

    if fams and not share:
        A("")
        A("  큰 버전 가족 예시:")
        for (d, stem, ext), members in fams[:6]:
            sz = sum(m[1] for m in members)
            A(f"    · '{stem}{ext}' × {len(members)}개 · {gb(sz)}")
            for m in sorted(members, key=lambda m: -m[1])[:3]:
                A(f"        {os.path.basename(m[0])[:52]:<54} "
                  f"{m[1]/2**20:8.1f} MB")

    # ── 보편 지문 (항목 21·25) — 비어도비 사용자를 받을 수 있는가
    th = analysis.get("thumbs")
    if th:
        A("")
        A("── 썸네일 (항목 21) ────────────────────────────────")
        A("  겉모습 비교가 되려면 썸네일이 나와야 합니다.")
        tot_ok = sum(v[0] for v in th.values())
        tot_try = sum(v[1] for v in th.values())
        A(row("★ 전체 성공률",
              f"{tot_ok}/{tot_try} ({tot_ok/max(tot_try,1)*100:.0f}%)"))
        for ext, (ok_, tried) in sorted(th.items(), key=lambda kv: -kv[1][1])[:12]:
            bar = "○" if ok_ == 0 else ("◐" if ok_ < tried else "●")
            A(row(f"  {bar} {ext or '(없음)'}", f"{ok_}/{tried}"))

    ph = analysis.get("phash")
    if ph:
        A("")
        A("── 겉모습만으로 묶기 (항목 25) ─────────────────────")
        A(row("지문 뽑기 성공", f"{ph['hashed']}/{ph['checked']} (실패 {ph['fail']})"))
        A(row("★ 겉모습으로 묶인 가족", f"{len(ph['families']):,} 개"))
        if ph["families"]:
            fb = sum(sum(m[1] for m in g) - max(m[1] for m in g)
                     for g in ph["families"])
            A(row("  가장 큰 것만 남기면", f"{gb(fb)}"))
            for g in (ph["families"][:3] if not share else []):
                A(f"      · {len(g)}개 · "
                  f"{os.path.basename(g[0][0])[:40]} 외")

    # ── AI 생성물 (항목 22·23)
    ai = analysis.get("ai")
    if ai and (ai["png_checked"] or ai["c2pa_checked"]):
        A("")
        A("── AI 생성 흔적 (항목 22·23) ───────────────────────")
        A(row("PNG 검사", f"{ai['png_checked']:,} 개"))
        A(row("★ 생성 파라미터 있음",
              f"{ai['png_with_params']:,} 개 "
              f"({ai['png_with_params']/max(ai['png_checked'],1)*100:.0f}%)"))
        A(row("C2PA 흔적 있음",
              f"{ai['c2pa_found']:,} / {ai['c2pa_checked']:,}"))
        if ai["keys"]:
            A("  발견된 텍스트 조각 이름:")
            for k, n in ai["keys"].most_common(6):
                A(f"      {k:<34} {n:,}")

    # ── 3D (항목 26~29)
    bk = analysis.get("backups")
    td = analysis.get("threed")
    bl = analysis.get("blend")
    if bk and bk["count"]:
        A("")
        A("── 자동 백업 (항목 26) ─────────────────────────────")
        A("  프로그램이 스스로 만든 직전 저장본입니다. 사용자 의도가 없어")
        A("  판단이 쉽고, 원본이 옆에 있으면 위험이 낮습니다.")
        A(row("★ 개수 · 용량", f"{bk['count']:,} 개 · {gb(bk['bytes'])}"))
        A(row("  원본이 옆에 있음", f"{bk['with_origin']:,} 개"))
        for ext, b in bk["by_ext"].most_common(8):
            A(row(f"    {ext}", gb(b)))
        for p, sz, has in (bk["samples"][:4] if not share else []):
            mark = ("원본 있음" if has else
                    ("원본 없음" if has is False else "확인 못함"))
            A(f"      {os.path.basename(p)[:44]:<46} "
              f"{sz/2**20:7.1f} MB  {mark}")

    if td and (td["native"] or td["exchange"]):
        A("")
        A("── 3D 파일 (항목 27·28) ────────────────────────────")
        A(row("네이티브 (blend·3dm·skp…)",
              f"{sum(td['native'].values()):,} 개 · {gb(td['native_bytes'])}"))
        for e, n in td["native"].most_common(6):
            A(row(f"    {e}", f"{n:,}"))
        A(row("교환 포맷 (stl·obj·step…)",
              f"{sum(td['exchange'].values()):,} 개 · {gb(td['exchange_bytes'])}"))
        for e, n in td["exchange"].most_common(6):
            A(row(f"    {e}", f"{n:,}"))

    if bl and (bl["ok"] or bl["compressed"] or bl["fail"]):
        A("")
        A(row(".blend 머리말 읽기", f"성공 {bl['ok']} · 압축 {bl['compressed']} "
                                   f"· 실패 {bl['fail']}"))
        A(row("★ 썸네일 들어 있음 (항목 29)",
              f"{bl['thumb']}/{max(bl['ok'],1)}"))
        if bl["versions"]:
            A(row("  Blender 버전",
                  ", ".join(f"{v}({n})" for v, n in bl["versions"].most_common(4))))

    # ── 간단한 세기 (항목 7·10·24)
    ct = analysis.get("counts")
    if ct:
        A("")
        A("── 그 밖 (항목 7·10·24) ────────────────────────────")
        A(row("참조를 가진 파일 (항목 7)",
              f"{ct['ref']:,} 개 · {gb(ct['ref_bytes'])}"))
        A(row("한글 이름 (항목 10)",
              f"{ct['hangul']:,} 개 ({ct['hangul_pct']:.0f}%)"))
        A(row("비어도비 원본 (항목 24)",
              f"{ct['nonadobe']:,} 개 · {gb(ct['nonadobe_bytes'])}"))

    # ── 항목 32: 튀는 폴더 (특수상황 감지)
    un = analysis.get("unusual")
    pf = analysis.get("profiles") or []
    if pf:
        A("")
        A("── 튀는 폴더 (항목 32) ─────────────────────────────")
        A("  직업을 열거하지 않고, 이 사람의 다른 폴더와 비교해 찾습니다.")
        A(row("프로파일 낸 폴더", f"{len(pf):,} 곳 (파일 5개 이상)"))
        has_bt = any(p["untouched"] is not None for p in pf)
        if not has_bt:
            A("  ※ 이 운영체제에는 생성 시각이 없어 '안 연 파일' 신호는 뺐습니다.")
        if len(pf) < MIN_FOLDERS_FOR_COMPARE:
            A(f"  → 폴더가 {len(pf)}곳뿐이라 '보통'의 기준을 못 잡습니다.")
            A(f"     (최소 {MIN_FOLDERS_FOR_COMPARE}곳 필요) 더 넓은 범위로 돌려 보세요.")
        elif not un:
            A("  → 유난히 튀는 폴더가 없습니다. 고르게 쓰고 계십니다.")
        for i, item in enumerate(un, 1):
            p = item["p"]
            name = ("(깊이"
                    + str(os.path.relpath(p["dir"], sv.root).count(os.sep))
                    + ")") if share else (os.path.basename(p["dir"])[:34]
                                          or "(최상위)")
            A("")
            A(f"  {i}. {name}   파일 {p['n']:,}개 · {gb(p['bytes'])} · {p['topext']}")
            for sig, value, z in item["hits"][:4]:
                A(f"       · {sig['label']} ({format_signal(sig, value)})")

    # ── 항목 31: 남의 작업물 (페르소나 진단의 핵심)
    ow = analysis.get("ownership")
    if ow:
        A("")
        A("── 남의 작업물이 섞여 있는가 (항목 31) ─────────────")
        A("  사용자가 정리 앱을 켤 때 가장 큰 걱정 중 하나입니다:")
        A("  \"내 것도 아닌 파일까지 다 섞어 옮기면 어떡하지\"")
        A("  ⚠ 표본 기준입니다 — 큰 파일 250개, 파일당 앞뒤 768KB 만 봅니다.")
        A("     실제보다 낮게 나옵니다. 얼마나 놓치는지는 아직 안 쟀습니다.")
        A(row("참조 보유 파일 검사",
              f"{ow['checked']:,} 개"
              + ("  (시간 제한으로 중단)" if ow.get("stopped_early") else "")))
        A(row("★ 다른 사람 경로가 박힌 파일",
              f"{ow['with_foreign']:,} 개 · {gb(ow['foreign_bytes'])}"))
        A(row("  등장한 다른 계정 수", f"{ow['foreign_count']:,} 명"))
        A(row("★ 이름에 강한 단서 (학생·외주 등)",
              f"{ow['strong_files']:,} 개 · {gb(ow['strong_bytes'])}"))
        for label, n in ow["strong"].most_common(8):
            A(row(f"    '{label}'", f"{n:,} 개"))
        if ow["weak"]:
            A(row("  약한 단서 (내 작업에도 붙는 말)",
                  f"{ow['weak_files']:,} 개"))
            A("      " + ", ".join(f"{lb}({n})"
                                   for lb, n in ow["weak"].most_common(6)))

    # ── 자주 쓰는 낱말 (페르소나 재료)
    # ── 자주 쓰는 낱말
    #
    #   ⚠ 이 절에는 실제 낱말이 그대로 나옵니다. 학생 이름만 적어 두는
    #     사람이라면 이름이 여기 올라옵니다. 보고서를 남에게 줄 일이
    #     생기면 이 절부터 확인하세요.
    vc = analysis.get("vocab")
    if vc and not share:
        A("")
        A("── 자주 쓰는 낱말 (페르소나 재료) ──────────────────")
        A("  이 사람이 무슨 일을 하는지 이름에서 읽습니다.")
        top = [(t, n) for t, n in vc.most_common(200)
               if n >= 3 and len(t) >= 2][:18]
        line = []
        for t, n in top:
            line.append(f"{t}({n})")
            if len(line) == 3:
                A("      " + "   ".join(f"{x:<20}" for x in line))
                line = []
        if line:
            A("      " + "   ".join(f"{x:<20}" for x in line))

    # ── 등급: 판단 난이도별로 나눈 것
    gr = analysis.get("grades")
    if gr:
        A("")
        A("── 등급 (뭘 남길지 판단하는 난이도) ─────────────────")
        A("  위로 갈수록 버튼 하나로 끝나고, 아래로 갈수록 화면이 필요합니다.")
        for key in ("1급", "2급", "3급", "4급"):
            g = gr.get(key)
            if not g:
                continue
            A("")
            A(f"  [{key}] {g['label']}")
            A(row(f"    묶음 · 정리 가능",
                  f"{g['families']:,}묶음 · {g['extra_files']:,}개 · "
                  f"{gb(g['bytes'])}"))

    dist = analysis.get("phash_dist")
    if dist:
        A("")
        A("  겉모습 지문 거리 분포 (2급을 나눌 수 있는가):")
        for k in ("0 (픽셀까지 같음)", "1~5 (미세한 차이)",
                  "6~10 (다른 버전)", "11+ (아마 다름)"):
            if dist.get(k):
                A(row(f"    {k}", f"{dist[k]:,} 쌍"))

    va = analysis.get("validity")
    if va:
        A("")
        A("  이 사람에게 유효한 신호 (모든 사용자에게 같지 않습니다):")
        for key, label in (("colorspace", "색공간(CMYK)"),
                           ("save_count", "저장 횟수"),
                           ("tool", "만든 프로그램")):
            info = va.get(key) or {}
            mark = "쓸 수 있음" if info.get("usable") else "안 씀"
            A(f"    {label:<14} {mark:<8} {info.get('why','')}")

    # ── 조언이 실제로 만들어지는가
    #   "이건 완전히 같아요" / "이건 포토샵으로 손보셨네요" 같은 문장을
    #   실제 데이터로 만들 수 있는지 확인합니다. 이게 3편의 핵심 기능입니다.
    ex = analysis.get("advice_examples")
    if ex:
        A("")
        A("  실제로 만들어진 조언 (3편의 핵심 기능):")
        for stem, msg, keep in (ex[:6] if not share else []):
            A(f"    · {stem[:40]}")
            A(f"        {msg[:100]}")
            if keep:
                A(f"        → {'뒤' if keep == 'b' else '앞'}쪽을 남기시는 게 좋겠습니다")
    elif analysis.get("lineage"):
        A("")
        A("  실제로 만들어진 조언: 없음")
        A("    (같은 가족 안에서 메타데이터 차이를 못 찾았습니다)")

    dl = analysis.get("dup_loc")
    if dl and dl["kinds"]:
        A("")
        A("  완전 중복이 어디에 있는가:")
        for label, n in dl["kinds"].most_common(6):
            mark = "  ← 이건 '중복' 이 아니라 '다시 생기는 것'" \
                   if label in ("앱이 만든 것", "개발 의존성", "캐시") else ""
            A(row(f"    {label}", f"{n:,} 개{mark}"))
        A("    많이 나온 폴더:")
        for d, n in (dl["dirs"].most_common(4) if not share else []):
            A(f"      {n:>4}개  {d}")

    # ── 판정
    A("")
    A("── 판정 ─────────────────────────────────────────────")
    # 3편이 회수할 수 있는 총량 = 버전 초과분 + 완전 중복 + 캐시 + 자동 백업
    #   자동 백업을 빼먹으면 3D 작업자 맥에서 실제보다 훨씬 작게 나옵니다.
    #   (원본이 옆에 있는 것만 셉니다 — 원본이 없으면 그건 백업이 아니라 유일본)
    backup_safe = bk["safe_bytes"] if bk else 0
    p3 = fam_excess + analysis['dups']['bytes'] + cache_total + backup_safe
    A(row("3편 예상 회수량", gb(p3)))
    A(row("  ├ 버전 초과분", gb(fam_excess)))
    A(row("  ├ 완전 중복", gb(analysis["dups"]["bytes"])))
    A(row("  ├ 캐시·렌더", gb(cache_total)))
    A(row("  └ 자동 백업", gb(backup_safe)))
    A(row("2편 대상 (1년 이상 미사용)", f"{sum(ab.get(k,0) for k in ['1~2년','2~3년','3~5년','5년 이상']):,} 개"))
    A("")

    verdict = []
    if p3 > 20 * 2**30:
        verdict.append("→ 3편 회수량이 20GB를 넘습니다. 3편을 먼저 만들 근거가 충분합니다.")
    elif p3 > 5 * 2**30:
        verdict.append("→ 3편 회수량이 의미 있는 수준입니다. 순서를 다시 검토할 만합니다.")
    else:
        verdict.append("→ 이 맥에서는 3편 회수량이 적습니다. 다른 맥(디자이너)에서도 재보세요.")

    if wb / max(ws, 1) > 0.3:
        verdict.append("→ '내보낸 결과물' 보조 증거가 어느 정도 있습니다.")
    else:
        verdict.append("→ '내보낸 결과물' 증거가 부족합니다. 계보에 더 의존해야 합니다.")

    lin = analysis.get("lineage")
    cmp_ = analysis.get("family_cmp")
    if lin and lin["checked"]:
        rate = lin["found"] / lin["checked"]
        if rate > 0.7:
            verdict.append(f"→ XMP 계보가 {rate*100:.0f}%에 있습니다. "
                           "파일 이름을 안 믿고도 가족을 묶을 수 있습니다. ★")
        elif rate > 0.3:
            verdict.append(f"→ XMP 계보가 {rate*100:.0f}%뿐입니다. "
                           "이름 기반과 병행해야 합니다.")
        else:
            verdict.append(f"→ XMP 계보가 {rate*100:.0f}%로 희박합니다. "
                           "다른 맥에서도 재보세요 (이 맥에 작업 파일이 적을 수 있음).")

    if cmp_ and cmp_["lineage_only"] > 0:
        # ⚠ 분모를 '이름이 찾은 것' 으로 잡았더니 이름이 0개일 때
        #   "20000% 더 찾았습니다" 가 나온 적이 있습니다.
        #   전체(둘 중 하나라도 찾은 파일) 대비 비중으로 바꿉니다.
        total_found = (cmp_["lineage_only"] + cmp_["name_only"]
                       + cmp_["both"]) or 1
        gain = cmp_["lineage_only"] / total_found
        if gain > 0.2:
            verdict.append(
                f"→ ★ 가족으로 묶인 파일 중 {gain*100:.0f}% 는 "
                f"계보로만 찾았습니다 ({cmp_['lineage_only']:,}개). "
                "이름만 봤으면 놓쳤을 파일이고, 3편의 존재 이유가 이 숫자입니다.")
        else:
            verdict.append("→ 계보와 이름의 결과가 비슷합니다. "
                           "이 사용자는 이름을 비교적 잘 관리하고 있습니다.")

    psd = analysis.get("psd")
    if psd and psd["ok"] >= 10:
        cmyk = psd["modes"].get("CMYK", 0)
        verdict.append(f"→ PSD 헤더를 {psd['ok']:,}개 읽었습니다 "
                       f"(CMYK {cmyk}개 — 인쇄 작업 비중).")

    # 항목 21 — 보편 지문을 바닥에 깔 수 있는가
    if th:
        # ⚠ 전체 평균을 쓰면 오해가 생깁니다.
        #   이미지 계열이 100% 인데 Unity 파일이 끌어내려 59% 로 나온 적이 있습니다.
        #   확장자별로 되는 것과 안 되는 것을 나눠서 보여 줍니다.
        works = [e for e, (o, t) in th.items() if t and o / t >= 0.7]
        fails = [e for e, (o, t) in th.items() if t and o / t < 0.3]
        tot_ok = sum(v[0] for v in th.values())
        tot_try = max(sum(v[1] for v in th.values()), 1)
        r = tot_ok / tot_try
        if works:
            verdict.append("→ 썸네일이 나오는 확장자: " + " ".join(sorted(works)))
        if fails:
            verdict.append("   안 나오는 확장자: " + " ".join(sorted(fails))
                           + " (이건 기하 지문 등 다른 경로가 필요합니다)")
        if r > 0.7:
            verdict.append(f"→ 썸네일이 {r*100:.0f}%에서 나옵니다. "
                           "겉모습 비교를 바닥에 깔 수 있습니다 — "
                           "비어도비 사용자도 받을 수 있습니다. ★")
        else:
            verdict.append(f"→ 썸네일이 {r*100:.0f}%뿐입니다. "
                           "안 나오는 확장자를 확인하고 기하 지문 등 "
                           "다른 경로를 검토하세요.")

    # 항목 26 — 3D 첫 기능이 확정되는가
    if bk and bk["safe_bytes"] > 2 * 2**30:
        verdict.append(f"→ ★ 안전하게 지울 수 있는 자동 백업만 {gb(bk['safe_bytes'])}입니다. "
                       "3D 대응의 첫 기능이 확정됩니다 (판단 쉽고 위험 낮음).")
    elif bk and bk["count"]:
        verdict.append(f"→ 자동 백업 {gb(bk['safe_bytes'])}(원본 확인분). "
                       "이 맥에서는 작지만 3D 작업자 맥에서 다시 재보세요.")

    # 항목 28 — 3D 대응의 시작점
    if td and sum(td["exchange"].values()) > sum(td["native"].values()):
        verdict.append("→ 3D는 교환 포맷(stl·obj·step)이 더 많습니다. "
                       "파싱이 쉬운 쪽이라 여기서 시작하면 됩니다.")

    # 항목 22 — AI 크리에이터를 초기 타깃에 넣을 것인가
    if ai and ai["png_with_params"] > 20:
        verdict.append(f"→ 생성 파라미터가 있는 PNG가 "
                       f"{ai['png_with_params']:,}개입니다. "
                       "AI 크리에이터 대응 가치가 확인됩니다.")

    # 항목 10 — 한국어 설계가 필요한가
    if ct and ct["hangul_pct"] > 30:
        verdict.append(f"→ 한글 이름이 {ct['hangul_pct']:.0f}%입니다. "
                       "이름 정규화와 쿼리 해석을 한국어 기준으로 설계하세요.")

    # 항목 7 — 참조 그래프를 언제 만들 것인가
    if ct and ct["ref"] > 200:
        verdict.append(f"→ 참조를 가진 파일이 {ct['ref']:,}개입니다. "
                       "참조 그래프를 이동·삭제보다 먼저 만들어야 합니다.")

    # 항목 31 — 소유 구분 단계가 필요한가
    # 항목 31 — 남의 작업물 (참고 수치)
    #
    #   ⚠ 이 숫자로 결론을 내리지 마세요.
    #     초기 표본이 한 직장(유학원) 동료들이라, 전원에게서 이 신호가
    #     나오는 것은 시장의 특성이 아니라 표본의 특성일 수 있습니다.
    #     직업 경로가 다른 사용자에게서도 나와야 일반화할 수 있습니다.
    if ow and ow["total_files"]:
        path_r = ow["with_foreign"] / max(ow["checked"], 1)
        name_r = ow["strong_files"] / ow["total_files"]
        if path_r > 0.1 or name_r > 0.05:
            verdict.append(
                f"→ 남의 작업물 신호가 있습니다 "
                f"(다른 계정 경로 {path_r*100:.0f}%, "
                f"이름 강한 단서 {name_r*100:.0f}%). "
                "※ 이 사람의 직업 특성일 수 있습니다. 직업이 다른 사용자에게서도 "
                "나오는지 확인한 뒤에 기능으로 만들지 결정하세요.")
        elif ow["with_foreign"] or ow["strong_files"]:
            verdict.append("→ 남의 작업물이 조금 섞여 있습니다. (참고)")
        else:
            verdict.append("→ 남의 작업물 신호가 거의 없습니다. "
                           "이 사람에게는 소유 구분이 불필요합니다.")

    if pct < 40:
        verdict.append("→ 손댈 수 있는 파일이 40% 미만입니다. 홈 전체 스캔은 낭비입니다.")

    if len(sv.files) > 200_000:
        verdict.append("→ 파일이 20만 개를 넘습니다. 2편은 Spotlight 없이는 불가능합니다.")

    for v in verdict:
        A("  " + v)

    ch = analysis.get("cache")
    if ch and ch.summary():
        A("")
        A("── 저장해 둔 것 활용 ────────────────────────────────")
        A("  " + ch.summary())
        A("  (이 맥은 큰 파일 첫 읽기에 백신 검사가 붙어 아주 느립니다.")
        A("   한 번 읽은 것을 저장해 두면 다음부터는 그 비용이 없습니다)")

    sl = analysis.get("steplog")
    if sl:
        L.extend(sl.report_lines())

    A("")
    A("=" * 68)
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────

def selftest():
    """이 파일이 최신인지 몇 가지 규칙으로 확인합니다.

    왜 필요한가:
      고친 파일을 드렸는데도 옛 동작이 나오는 일이 반복됐습니다
      (다운로드가 덮어쓰기 안 되고 p0_survey-1.py 로 저장되는 등).
      버전 문자열만으로는 어느 수정까지 들어갔는지 알 수 없어서,
      실제 동작을 확인합니다. 1편의 selftest.py 와 같은 발상입니다.
    """
    # 시험 사례는 언어마다 다릅니다 → 사전에서 가져옵니다
    cases = getattr(LANG, "SELFTEST_CASES", None) or [
        # (입력, 기대, 무엇을 확인하는가)
        ("스크린샷 2024-05-02 오후 3.50.51", "스크린샷 2024 05 02 오후 3 50 51",
         "스크린샷 날짜 유지 (서로 다른 화면이 한 가족이 되면 안 됨)"),
        ("동영상 2023. 11. 6. 오후 6.43", "동영상 2023 11 6 오후 6 43",
         "동영상 날짜 유지"),
        ("IMG_4821", "img 4821", "카메라 파일 번호 유지"),
        ("포스터_진짜최종", "포스터", "버전 표시는 벗김"),
        ("로고_20240301", "로고", "보통 파일의 날짜는 벗김"),
        ("제안서_최종", "제안서", "'안' 이 낱말 안에서 안 벗겨짐"),
        (unicodedata.normalize("NFD", "스크린샷 2024-05-02 오후 3.50.51"),
         "스크린샷 2024 05 02 오후 3 50 51",
         "★ macOS 자모 분리(NFD) 파일명 처리"),
        (unicodedata.normalize("NFD", "포스터_진짜최종"), "포스터",
         "★ NFD 한글에서 버전 표시 벗기기"),
    ]
    print(f"\n  자가진단 — 이 파일이 최신인지 확인합니다 ({VERSION})\n")
    bad = 0
    for src, want, why in cases:
        got = normalize_stem(src)
        ok = got == want
        if not ok:
            bad += 1
        print(f"    {'통과' if ok else '실패'}  {why}")
        if not ok:
            print(f"          입력 {src!r}")
            print(f"          기대 {want!r}")
            print(f"          실제 {got!r}")
    if bad:
        print(f"\n  ⚠ {bad}개 실패 — 옛 파일입니다. 다시 받으세요.\n")
    else:
        print(f"\n  전부 통과 — 최신 파일입니다.\n")
    return bad


def main():
    """P0 실측 전체를 순서대로 돌립니다.

    순서: 환경 → 훑기 → 캐시 → 가벼운 세기 → 파일 내부 읽기 → 외부 프로세스
    뒤로 갈수록 느려서, --quick 과 --no-probe 로 뒤쪽부터 끌 수 있습니다.
    """
    ap = argparse.ArgumentParser(description="P0 실측 (읽기 전용)")
    ap.add_argument("--path", default="~", help="훑을 폴더 (기본: 홈)")
    ap.add_argument("--quick", action="store_true", help="해시 검사 생략")
    ap.add_argument("--no-probe", action="store_true",
                    help="파일 내부(XMP·PSD·AI) 검사 생략")
    ap.add_argument("--cache", default="p0_cache.json",
                    help="파일에서 읽은 것을 저장해 둘 경로. 중간에 멈춰도 "
                         "여기까지는 남고, 다시 돌리면 안 읽습니다")
    ap.add_argument("--no-cache", action="store_true",
                    help="저장해 둔 것을 무시하고 전부 다시 읽습니다")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="예상 시간을 묻지 않고 그대로 진행합니다")
    ap.add_argument("--steplog", default="p0_steps.log",
                    help="단계별 소요 시간 기록 경로. 중간에 멈춰도 "
                         "여기까지는 남습니다")
    ap.add_argument("--ownership", action="store_true",
                    help="[항목 31] 남의 작업물 신호를 함께 검사합니다. "
                         "표본 기준이라 실제보다 낮게 나오고 느립니다. "
                         "지금 순서 판단에는 쓰이지 않아 기본은 꺼져 있습니다")
    ap.add_argument("--testset", default="p0_testset.md",
                    help="[항목 14·15] 규칙 vs LLM 채점표 경로 "
                         "(파일 이름 포함 — 공유 금지)")
    ap.add_argument("--json", help="원시 수치 저장 (개인정보 포함 — 공유 금지)")
    ap.add_argument("--out", default="p0_report.txt", help="보고서 경로")
    ap.add_argument("--max-files", type=int, default=400_000)
    ap.add_argument("--version", action="version",
                    version=f"p0_survey {VERSION}")
    ap.add_argument("--share", action="store_true",
                    help="공유용 — 파일 이름·경로가 나오는 절을 빼고 "
                         "숫자만 담습니다. 채점표도 안 만듭니다")
    ap.add_argument("--selftest", action="store_true",
                    help="이 파일이 최신인지 확인만 하고 끝냅니다 (몇 초)")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(1 if selftest() else 0)

    t0 = time.time()
    # 어느 버전을 돌리고 있는지 화면 맨 처음에 알립니다.
    # (보고서 안에만 적어 뒀더니 "p0-8 이 어디 찍히냐" 는 질문을 받았습니다)
    print(f"\n  P0 실측  {VERSION}   —  파일을 바꾸지 않습니다", flush=True)
    if normalize_stem("스크린샷 2024-05-02 오후 3.50.51") != \
            "스크린샷 2024 05 02 오후 3 50 51":
        print("  ⚠ 옛 파일입니다. --selftest 로 확인하고 다시 받으세요.",
              flush=True)
    print(flush=True)
    cache = ReadCache(args.cache, enabled=not args.no_cache)
    globals()["_CACHE"] = cache      # Ctrl+C 때 저장하려고 전역에 둡니다
    log = StepLog(args.steplog)

    # 각 단계를 StepLog.run 으로 감싸면 시간이 자동으로 기록되고,
    # 끝날 때마다 p0_steps.log 에 이어 써집니다 (중간에 멈춰도 남습니다).
    env = log.run("환경 확인", lambda: environment())

    sv = Survey(args.path, quick=args.quick, max_files=args.max_files)
    log.run("파일 훑기", lambda: (sv.walk(), sv.files)[1])
    log.run("캐시·렌더 확인", lambda: sv.scan_known_caches())

    families = log.run("버전 가족 묶기", lambda: cluster_versions(sv.files))
    dup_cands = [f for f in sv.files if f[1] >= (1 << 20)] if not args.quick else []
    dups = run_with_estimate(
        log, "완전 중복 검사", dup_cands,
        lambda pr: find_duplicates(sv.files, quick=args.quick, progress=pr,
                                   cache=cache),
        auto_yes=args.yes)
    if dups is None:
        dups = find_duplicates([], quick=True)
    backups = log.run("자동 백업 찾기", lambda: probe_backups(sv.files))
    threed = log.run("3D 파일 세기", lambda: probe_3d(sv.files))
    counts = log.run("참조·한글·비어도비 세기", lambda: probe_counts(sv.files))
    blend = log.run(".blend 머리말", lambda: probe_blend(sv.files))
    vocab = log.run("낱말 빈도", lambda: build_token_vocab(sv.files))
    fprofiles = log.run("폴더 모양", lambda: profile_folders(sv.files, sv.btimes))
    unusual = find_unusual_folders(fprofiles)

    lineage = family_cmp = psd = ai = ownership = None
    thumbs = phash = None

    if args.ownership and not args.no_probe:
        ownership = log.run("남의 작업물 (항목 31)",
                            lambda: probe_ownership(sv.files, vocab),
                            note="표본 기준 — 실제보다 낮게 나옴")

    if not args.no_probe:
        xmp_cands = sorted([f for f in sv.files if f[3] in XMP_PROBE_EXTS],
                           key=lambda f: -f[1])[:6000]
        lineage = run_with_estimate(
            log, "계보 (XMP)", xmp_cands,
            lambda pr: probe_lineage(sv.files, progress=pr, cache=cache),
            auto_yes=args.yes,
            note="큰 것부터 — 회수량 계산에 필요")
        if lineage:
            family_cmp = compare_family_methods(families,
                                                lineage["lineage_families"])
        psd = log.run("PSD 헤더", lambda: probe_psd(sv.files))
        ai = log.run("AI 생성 흔적", lambda: probe_ai_metadata(sv.files))

    # 아래 둘은 외부 프로세스(qlmanage·sips)를 씁니다. 가장 느립니다.
    if not args.no_probe and not args.quick:
        workdir = os.path.join(tempfile.gettempdir(), "p0_probe")
        os.makedirs(workdir, exist_ok=True)
        try:
            thumbs = log.run("썸네일 (qlmanage)",
                             lambda: probe_thumbnails(sv.files, workdir),
                             note="외부 프로세스 · 확장자별 표본")
            phash = log.run("겉모습 지문 (sips)",
                            lambda: probe_phash_families(sv.files, workdir),
                            note="외부 프로세스")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    grades = grade_families(sv.files, dups, families, lineage,
                            (phash or {}).get("families"))
    validity = measure_signal_validity(psd, lineage)
    advice = build_advice_examples(sv.files, lineage, cache, validity)
    dist = probe_phash_distance((phash or {}).get("families"))
    duploc = probe_dup_locations(dups, sv.root)

    analysis = {
        "grades": grades, "phash_dist": dist, "dup_loc": duploc,
        "advice_examples": advice, "validity": validity,
        "families": families, "dups": dups,
        "deriv": find_derivatives(sv.files),
        "names": name_quality(sv.files), "age": age_buckets(sv.files),
        "lineage": lineage, "family_cmp": family_cmp, "psd": psd,
        "thumbs": thumbs, "phash": phash, "ai": ai,
        "backups": backups, "threed": threed, "blend": blend,
        "counts": counts, "vocab": vocab,
        "ownership": ownership,
        "profiles": fprofiles, "unusual": unusual,
        "steplog": log,
        "cache": cache,
    }

    cache.save()
    report = build_report(env, sv, analysis, time.time() - t0,
                          share=args.share)
    print("\n" + report)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n보고서 저장: {args.out}")

    if args.share:
        print("\n공유 모드입니다.")
        print("  · 파일 이름과 경로가 나오는 절을 빼고 숫자만 담았습니다")
        print("  · 채점표는 만들지 않았습니다")
        print("보고서 파일 하나만 전달하시면 됩니다.")
        return

    # [항목 14·15] 사람이 손으로 채점할 시험지
    try:
        n = write_manual_testset(sv.files, args.testset)
        print(f"채점표 저장: {args.testset} ({n}개)  "
              f"⚠ 파일 이름 포함 — 공유하지 마세요")
    except (OSError, ValueError) as e:
        print(f"채점표를 못 만들었습니다: {e}")

    if args.json:
        raw = {
            "version": VERSION,
            "env": env,
            "counts": {
                "files": len(sv.files),
                "total_bytes": sv.total_bytes,
                "cloud_bytes": sv.cloud_bytes,
                "bundle_bytes": sv.bundle_bytes,
            },
            "families": [
                {"stem": k[1], "ext": k[2], "n": len(v),
                 "bytes": sum(m[1] for m in v)}
                for k, v in families[:300]
            ],
            "cache": {k: v for k, v in sv.cache_hits.items()},
            "lineage": ({k: v for k, v in analysis["lineage"].items()
                         if k not in ("lineage_families", "tools")}
                        if analysis.get("lineage") else None),
            "family_cmp": analysis.get("family_cmp"),
            "errors": dict(sv.errors),
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        print(f"원시 수치 저장: {args.json}  ⚠ 파일 이름 포함 — 공유하지 마세요")


if __name__ == "__main__":
    # Ctrl+C 로 멈춰도 지금까지 읽어 둔 것은 남깁니다.
    #   3시간 읽다가 멈췄는데 전부 날아가면, 다시 3시간을 내야 합니다.
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  멈췄습니다.")
        c = globals().get("_CACHE")
        if c is not None:
            c.save()
            print(f"  지금까지 읽어 둔 것은 {c.path} 에 남겼습니다.")
            print("  다시 돌리면 그 부분은 건너뜁니다.")
        sys.exit(130)
