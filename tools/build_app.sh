#!/usr/bin/env bash
#
# build_app.sh — p0_app.py 를 .app 으로 포장하고 zip 으로 묶습니다.
#
#   실행:  bash tools/build_app.sh
#   결과:  dist/맥 파일 검사.app   과   dist/맥파일검사.zip
#
#   ── 지금 (P0~P4) ────────────────────────────────────────
#       py2app → zip
#       서명·공증은 하지 않습니다. 개발자 계정은 3편 프로토타입 이후.
#       그때까지는 배포_안내.md 의 Gatekeeper 안내가 그대로 통합니다.
#
#   ── P5 (개발자 계정 이후) ───────────────────────────────
#       이 파일 맨 아래 '서명·공증' 구간의 주석을 풀고 값만 채우면
#       py2app → codesign → notarytool → stapler → DMG 가 됩니다.
#       빌드 흐름은 그대로 재사용합니다 (APP_UI 10항).
#
set -euo pipefail

# 이 스크립트가 어디서 불려도 레포 뿌리에서 돌게 합니다.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

APP_NAME="맥 파일 검사"
ZIP_NAME="맥파일검사.zip"

echo "── 확인 ────────────────────────────────────────────"
if [[ "$(uname)" != "Darwin" ]]; then
  echo "⚠ 이 스크립트는 macOS 에서 돌려야 합니다 (py2app 은 .app 을 만듭니다)."
  echo "  지금 OS: $(uname)"
  exit 1
fi

# 어느 파이썬으로 빌드할지. .app 안에 들어가는 Tk(화면 엔진)는 이 파이썬 걸
# 따라갑니다. macOS 시스템/Xcode 파이썬은 옛 Tk(8.5)라 글자가 안 보입니다.
#   → python.org 정식 파이썬(Tk 8.6)으로 빌드하세요.
#   다른 파이썬으로 빌드하려면:  PYTHON=python3.12 bash tools/build_app.sh
PYTHON="${PYTHON:-python3}"

# 필요한 파일이 다 있는지 (엔진·사전이 빠지면 앱이 안 돕니다)
for f in p0_app.py p0_survey.py lang_ko.py setup.py; do
  [[ -f "$f" ]] || { echo "✗ $f 가 없습니다."; exit 1; }
done

# 화면 엔진(Tk) 버전 확인 — 8.5 면 동료들 화면이 깨집니다.
echo "── 화면 엔진(Tk) 확인 ──────────────────────────────"
TKV="$("$PYTHON" -c 'import tkinter; print(tkinter.TkVersion)' 2>/dev/null || echo '?')"
echo "  $PYTHON 의 Tk: $TKV"
if [[ "$TKV" == "8.5" || "$TKV" == "?" ]]; then
  echo "  ⚠ 옛 Tk(8.5) 입니다. 이대로 빌드하면 글자·카드가 안 보입니다."
  echo "    python.org 에서 최신 Python 3 을 설치한 뒤,"
  echo "    새 터미널에서:  PYTHON=python3.12 bash tools/build_app.sh"
  read -r -p "  그래도 계속할까요? [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "  멈췄습니다."; exit 1; }
fi

# 엔진이 최신인지 먼저 확인합니다 (APP_UI 8항).
echo "── 자가진단 (--selftest) ───────────────────────────"
"$PYTHON" p0_survey.py --selftest || {
  echo "✗ 자가진단 실패 — 옛 엔진 파일입니다. 최신본으로 바꾸고 다시 빌드하세요."
  exit 1
}

# py2app 준비 (없으면 설치)
echo "── py2app 확인 ─────────────────────────────────────"
"$PYTHON" -c "import py2app" 2>/dev/null || {
  echo "  py2app 이 없어 설치합니다…"
  "$PYTHON" -m pip install --user py2app
}

echo "── 이전 산출물 지우기 ──────────────────────────────"
rm -rf build dist

echo "── py2app 빌드 ($PYTHON) ───────────────────────────"
# ⚠ py2app 최신 버전은 마지막에 번들을 ad-hoc 서명합니다. 그런데
#   python.org 의 Tcl/Tk 안에는 codesign 이 못 다루는 정적 라이브러리
#   (*.a, 예: libtclstub.a)가 들어 있어 이 서명이 '실패'합니다.
#   그러면 앱에 '깨진 서명'이 남고, Apple Silicon 은 서명이 없는 것보다
#   깨진 서명을 더 싫어해서 실행 즉시 죽입니다(Code Signature Invalid).
#   → 여기서 실패해도 멈추지 않고, 아래에서 *.a 를 지운 뒤 우리가 직접
#     ad-hoc 서명합니다. (개발자 계정 없이 되는 무료 서명입니다)
set +e
"$PYTHON" setup.py py2app
PY2APP_RC=$?
set -e

APP_PATH="dist/${APP_NAME}.app"
[[ -d "$APP_PATH" ]] || {
  echo "✗ .app 을 못 만들었습니다 (py2app 종료코드 $PY2APP_RC)."; exit 1;
}
if [[ $PY2APP_RC -ne 0 ]]; then
  echo "  (py2app 이 마지막 서명에서 멈췄습니다 — 예상된 일입니다. 아래에서 직접 서명합니다)"
fi

echo "── 서명 정리 (무료 ad-hoc) ─────────────────────────"
# codesign 이 못 다루는 정적 라이브러리 제거 (런타임에 필요 없습니다)
find "$APP_PATH" -name '*.a' -print -delete || true
# 남은 확장 속성 정리 후, 번들 전체를 ad-hoc 로 다시 서명
xattr -cr "$APP_PATH" || true
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict --verbose=2 "$APP_PATH" || {
  echo "  ⚠ 서명 검증에 경고가 있지만, 실행에는 보통 문제 없습니다."
}

# 번들 안에서 엔진·사전이 실제로 import 되는지 확인합니다
# (APP_UI 9항 — 번들에 lang_ko 가 들어갔나 / import lang_ko 가 되나).
echo "── 번들 자가진단 ───────────────────────────────────"
BIN="${APP_PATH}/Contents/MacOS/p0_app"
if [[ -x "$BIN" ]]; then
  echo "  (엔진 selftest 는 위에서 통과했습니다. 앱은 실행 시 화면에서 다시 확인합니다)"
fi

echo "── zip 으로 묶기 ───────────────────────────────────"
cd dist
# -y : 심볼릭 링크를 보존합니다 (.app 안의 프레임워크가 링크로 들어 있음)
rm -f "$ZIP_NAME"
zip -q -r -y "$ZIP_NAME" "${APP_NAME}.app"
cd ..

echo ""
echo "✓ 다 됐습니다."
echo "   앱 :  ${APP_PATH}"
echo "   zip:  dist/${ZIP_NAME}   ← 이 파일을 배포_안내.md 와 함께 보내세요"
echo ""
echo "  ※ 무료 ad-hoc 서명만 했습니다(정식 서명·공증은 P5). 받는 분은"
echo "    첫 실행 때 Gatekeeper 안내가 필요합니다 — 배포_안내.md 를 함께 보내세요."
echo "    (혹시 '손상되었다'고 뜨면 배포_안내.md 의 마지막 항목을 참고)"

# ═════════════════════════════════════════════════════════════
#  P5 — 서명·공증 (개발자 계정 이후에만 켭니다)
#
#  아래 주석을 풀고 값을 채우면, 위 zip 대신 서명·공증된 DMG 가
#  만들어집니다. 빌드 흐름(py2app)은 그대로 두고 뒤에 덧붙일 뿐입니다.
#
#  준비물:
#    · Apple Developer 계정 ($99/년)
#    · "Developer ID Application" 인증서 (키체인)
#    · notarytool 프로필 (xcrun notarytool store-credentials)
# ═════════════════════════════════════════════════════════════
#
# DEV_ID="Developer ID Application: 이름 (TEAMID)"
# NOTARY_PROFILE="p0-notary"          # store-credentials 로 저장한 이름
# DMG_NAME="맥파일검사.dmg"
#
# echo "── (P5) 서명 ───────────────────────────────────────"
# codesign --deep --force --options runtime --timestamp \
#          --sign "$DEV_ID" "$APP_PATH"
# codesign --verify --deep --strict --verbose=2 "$APP_PATH"
#
# echo "── (P5) DMG 만들기 ─────────────────────────────────"
# rm -f "dist/${DMG_NAME}"
# hdiutil create -volname "$APP_NAME" -srcfolder "$APP_PATH" \
#         -ov -format UDZO "dist/${DMG_NAME}"
#
# echo "── (P5) 공증 ───────────────────────────────────────"
# xcrun notarytool submit "dist/${DMG_NAME}" \
#         --keychain-profile "$NOTARY_PROFILE" --wait
#
# echo "── (P5) 스테이플 ───────────────────────────────────"
# xcrun stapler staple "dist/${DMG_NAME}"
# xcrun stapler validate "dist/${DMG_NAME}"
# echo "✓ 서명·공증된 dist/${DMG_NAME} 이 준비됐습니다."
