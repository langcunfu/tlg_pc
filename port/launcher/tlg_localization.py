#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Independent Simplified-Chinese localization adapter for The Last Guardian (tlg_pc).

Keeps the upstream source untouched and free of translation forks so that pulling new
upstream commits stays clean. The adapter is applied at runtime (monkey-patches the
launcher and the game-prepare path), the same strategy the XDGAME builds use.

  * in-game Simplified Chinese: the 1.03 engine gates its language table by the
    AppContent region. TLG_LANGUAGE=11 is the real Simplified-Chinese id, but the EU
    content.bin (region params 3,2,1612131829) does not recognize it and the engine
    falls back to English. When id 11 + the zhc archives are present, the adapter
    switches out/content.bin to the official China (CUSA04937 v1.03) region params
    3,4,1612131833 so id 11 really loads trico_zhc.psarc. It also exposes zh_CN in the
    language table and defaults to it when the disc ships the Chinese archives.
  * launcher UI: the Qt and Tk launchers keep their upstream (en, pt) tables; the
    adapter translates text at render time from a standalone zh dictionary and injects
    a Chinese-capable font.

One new file next to this one carries the translations: tlg_localization-zh.json
(an {English text: Simplified Chinese} map, swept from the upstream UI strings).

Install:
    import tlg_localization as zh
    zh.install()                       # applies to whatever launcher imports it
"""
import json
import os
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_installed = False
_PAIRS = {}


def _load_pairs():
    global _PAIRS
    if not _PAIRS:
        try:
            _PAIRS = json.loads((HERE / 'tlg_localization-zh.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            _PAIRS = {}
    return _PAIRS


def _system_is_chinese(environ=None):
    env = os.environ if environ is None else environ
    for name in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        value = env.get(name)
        if value:
            return value.lower().startswith(('zh', 'cmn'))
    try:
        import locale
        return (locale.getlocale()[0] or '').lower().startswith(('zh', 'cmn'))
    except (ValueError, OSError):
        return False


def _is_chinese(settings):
    choice = settings.get('ui_language', 'auto')
    if choice == 'zh_CN':
        return True
    if choice == 'pt_BR' or choice == 'en_US':
        return False
    return _system_is_chinese()   # 'auto'


def _zhc_present(game):
    """The Chinese archives the official China disc ships (sizes match the XDGAME probe)."""
    if not game:
        return False
    try:
        return ((Path(game) / 'trico_zhc.psarc').stat().st_size == 269953024 and
                (Path(game) / 'trico_zhcp.psarc').stat().st_size == 275026)
    except OSError:
        return False


# -------------------------------------------------------------------------------------- core
def install_core():
    """In-game Simplified Chinese: language table + content.bin China region switch."""
    import tlg_run as run

    _load_pairs()

    # -- language table: expose zh_CN (id 11) for the EU disc that ships the zhc archives.
    original_languages = run.game_languages
    original_default = run.default_language

    def languages(game_dir=''):
        table = dict(original_languages(game_dir))
        if _zhc_present(game_dir):
            table['zh_CN'] = 11
        return table

    def default_language(game_dir=''):
        if _zhc_present(game_dir):
            return 'zh_CN'
        return original_default(game_dir)

    run.game_languages = languages
    run.default_language = default_language

    # GAME_LANGUAGES is the global key set used by choices / resolution; add zh_CN.
    if 'zh_CN' not in run.GAME_LANGUAGES:
        run.GAME_LANGUAGES = dict(run.GAME_LANGUAGES, zh_CN=11)

    # -- content.bin: switch the AppContent region to the official China parameters so the
    #    engine recognizes id 11 (TLG_LANGUAGE). Runs after content_profile.py wrote the EU one.
    original_prepare = run.prepare

    def prepare(settings, log=print):
        modules = original_prepare(settings, log)
        try:
            if run.build_env(settings).get('TLG_LANGUAGE') == '11' and _zhc_present(settings.get('game_dir')):
                p = run.OUT_DIR / 'content.bin'
                raw = p.read_bytes()
                eu = struct.pack('<8s5I', b'G3CONT01', 3, 2, 1612131829, 0, 0)
                cn = struct.pack('<8s5I', b'G3CONT01', 3, 4, 1612131833, 0, 0)
                if raw == eu:
                    p.write_bytes(cn)
                    log('Chinese language region: official China (CUSA04937) params; id 11 loads trico_zhc')
        except OSError:
            pass
        return modules

    run.prepare = prepare


def install_qt():
    """Qt launcher UI Simplified Chinese (patch the translator and the font)."""
    install_core()
    from qt import logic

    logic.LANGS = ('zh_CN', 'pt_BR', 'en_US')

    # game-language names: keep upstream names; zh_CN is already present as '简体中文'.
    if 'zh_CN' not in logic.GAME_LANGUAGE_NAMES:
        logic.GAME_LANGUAGE_NAMES = dict(logic.GAME_LANGUAGE_NAMES, zh_CN='简体中文')

    # -- Translator.__call__: when the interface is Chinese, look the English text up in the
    #    zh dictionary (strings.json first, then the Tk TEXT table); otherwise keep upstream.
    original_init = logic.Translator.__init__
    original_call = logic.Translator.__call__
    original_pt = logic.Translator.pt

    def _init(self, settings, strings=None, tk=None):
        original_init(self, settings, strings, tk)

    def _call(self, key, **fmt):
        text = original_call(self, key, **fmt)
        if not _is_chinese(self.settings):
            return text
        # text is the English (or pt) string the upstream translator picked; if it has a
        # Chinese translation in the dictionary, return that instead.
        cn = _PAIRS.get(text)
        if cn is not None:
            try:
                return cn.format(**fmt)
            except (KeyError, IndexError):
                return cn
        return text

    logic.Translator.__init__ = _init
    logic.Translator.__call__ = _call

    # -- pair((en, pt)): used for choice labels; translate when Chinese.
    original_pair = logic.Translator.pair

    def _pair(self, pair):
        if _is_chinese(self.settings) and len(pair) >= 1:
            cn = _PAIRS.get(pair[0])
            if cn is not None:
                return cn
        return original_pair(self, pair)

    logic.Translator.pair = _pair

    # -- Backend.lang: the QML language buttons compare `Backend.lang === modelData.code`.
    #    Upstream returns only pt_BR/en_US; report zh_CN when the interface is Chinese so the
    #    Chinese button lights up and `lang` re-evaluates on switch.
    try:
        from qt.backend import Backend
    except Exception:
        Backend = None
    if Backend is not None:
        original_lang = Backend.lang

        def _lang(self):
            if _is_chinese(self.settings):
                return 'zh_CN'
            return original_lang(self)

        Backend.lang = _lang

    # -- Chinese-capable fonts: the adapter only runs when Simplified Chinese is wanted, so
    #    prefer a system font that renders CJK (Microsoft YaHei on Windows, Noto Sans CJK on
    #    Linux) or a bundled CJK font in qt/fonts. CJK families also cover Latin, so switching
    #    the text family is safe in every language.
    try:
        from qt import app as qapp
    except Exception:
        qapp = None
    if qapp is not None:
        original_load = qapp.load_fonts
        _CJK = ('Microsoft YaHei', 'Microsoft YaHei UI', 'Noto Sans CJK SC', 'Noto Sans SC',
                'WenQuanYi Zen Hei', 'PingFang SC', 'Source Han Sans SC')

        def _load_fonts():
            display, text = original_load()
            from PySide6.QtGui import QFontDatabase, QFont
            # a bundled CJK font in qt/fonts wins if present (e.g. a user-provided .ttf)
            bundled = None
            for path in sorted((qapp.HERE / 'fonts').glob('*.ttf')):
                ident = QFontDatabase.addApplicationFont(str(path))
                if ident >= 0:
                    for name in QFontDatabase.applicationFontFamilies(ident):
                        if QFont(name).exactMatch():
                            bundled = name
                            break
                if bundled:
                    break
            family = bundled or next((n for n in _CJK if QFont(n).exactMatch()), None)
            return (display, family) if family else (display, text)

        qapp.load_fonts = _load_fonts


def install_classic():
    """Tk launcher UI Simplified Chinese (patch t() and pt())."""
    install_core()
    import tlg_launcher as launcher

    launcher.UI_LANGUAGES = ['auto', 'zh_CN', 'pt_BR', 'en_US']
    if 'zh_CN' not in launcher.UI_LANGUAGE_NAMES:
        launcher.UI_LANGUAGE_NAMES = dict(launcher.UI_LANGUAGE_NAMES, zh_CN='简体中文')

    original_pt = launcher.Launcher.pt
    original_t = launcher.Launcher.t

    def _pt(self):
        choice = self.settings.get('ui_language', 'auto')
        if choice == 'zh_CN':
            return False   # not Portuguese
        return original_pt(self)

    def _t(self, key, **fmt):
        text = original_t(self, key, **fmt)
        if not _is_chinese(self.settings):
            return text
        cn = _PAIRS.get(text)
        if cn is not None:
            try:
                return cn.format(**fmt)
            except (KeyError, IndexError):
                return cn
        return text

    launcher.Launcher.pt = _pt
    launcher.Launcher.t = _t


def install():
    """Apply in-game Chinese always, then the UI adapter for whichever launcher runs.

    Call it once at launcher entry (Qt and Tk entry points). `--play` / `--script` roles go
    through tlg_run directly; they only need install_core, which this always applies.
    """
    global _installed
    if _installed:
        return
    install_core()
    # Detect the running launcher from loaded modules, never from import side effects.
    if 'tlg_launcher_qt' in sys.modules or 'qt.logic' in sys.modules or 'qt.app' in sys.modules:
        install_qt()
    if 'tlg_launcher' in sys.modules:
        install_classic()
    _installed = True
