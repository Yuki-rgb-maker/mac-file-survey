#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup.py — py2app 으로 p0_app.py 를 .app 으로 포장합니다.

  빌드는 tools/build_app.sh 로 하세요. 이 파일은 그 스크립트가 부릅니다.

  ── 지금 담는 것 ─────────────────────────────────────────
    · p0_app.py    화면
    · p0_survey.py 엔진 (수정하지 않음)
    · lang_ko.py   한국어 사전  ← 번들에 꼭 들어가야 import 됩니다
                                  (APP_UI 9항 확인 항목)

  ── 서명·공증은 지금 안 합니다 ──────────────────────────
    개발자 계정은 3편 프로토타입 이후. 그때 tools/build_app.sh 의
    P5 구간(codesign → notarytool → stapler → DMG)만 켜면 됩니다.
    setup.py 는 바뀌지 않습니다.
"""

from setuptools import setup

APP = ["p0_app.py"]

# 엔진과 사전은 소스 모듈로 함께 넣습니다. py2app 이 import 를 따라가지만,
# lang_ko 는 p0_survey 안에서 try/except 로 늦게 import 하므로 명시합니다.
DATA_FILES = []

OPTIONS = {
    "argv_emulation": False,        # ⚠ Tkinter 와 argv_emulation 을 같이 쓰면
                                    #    실행 직후 멈추는 일이 있습니다. 끕니다.
    "includes": ["p0_survey", "lang_ko"],
    "packages": ["tkinter"],
    "plist": {
        # ⚠ CFBundleName 은 ASCII 로 둡니다. py2app 은 이 값으로 .app 폴더와
        #   'Contents/MacOS/<실행파일>' 이름을 정하는데, 여기에 공백·한글이
        #   들어가면 codesign 이 그 실행파일을 못 서명합니다
        #   ("code object is not signed at all"). 화면에 보이는 한글 이름은
        #   아래 CFBundleDisplayName 이 담당하고, .app 폴더 이름은
        #   build_app.sh 가 서명을 끝낸 뒤 한글로 바꿉니다(폴더 이름은
        #   서명에 포함되지 않아 안전).
        "CFBundleName": "MacFileSurvey",
        "CFBundleExecutable": "MacFileSurvey",
        "CFBundleDisplayName": "맥 파일 검사",
        "CFBundleIdentifier": "com.macfilesurvey.p0",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # 조사 도구라 백그라운드로 돌 일이 없습니다. 창 하나짜리 앱.
        "LSApplicationCategoryType": "public.app-category.utilities",
        # ── 권한 안내 문구 (APP_UI 6항) ───────────────────────
        #   macOS 가 폴더 접근을 물을 때 이 문장을 보여 줍니다.
        #   앱 이름으로 뜨므로 '터미널' 이 아니라 이 앱이 묻는 게 됩니다.
        "NSDesktopFolderUsageDescription":
            "검사 결과 파일을 바탕화면에 저장합니다.",
        "NSDocumentsFolderUsageDescription":
            "문서 폴더의 파일 상태를 셉니다. 읽기만 하고 바꾸지 않습니다.",
        "NSDownloadsFolderUsageDescription":
            "다운로드 폴더의 파일 상태를 셉니다. 읽기만 하고 바꾸지 않습니다.",
    },
}

setup(
    # ⚠ 번들·실행파일 이름은 ASCII 로 둡니다("MacFileSurvey").
    #   실행파일 이름에 공백·한글이 들어가면 codesign 이
    #   "code object is not signed at all" 로 서명을 실패합니다.
    #   화면에 보이는 이름은 plist 의 CFBundleDisplayName(한글) 이 담당하고,
    #   .app 폴더 이름은 build_app.sh 가 서명 뒤 한글로 바꿉니다
    #   (폴더 이름은 서명에 포함되지 않아 안전).
    app=APP,
    name="MacFileSurvey",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
