"""Local legacy Word conversion; original bytes remain the source identity."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET

from .markdown_converter import ConversionFailed, ConverterUnavailable


class LegacyConverterUnavailable(ConverterUnavailable):
    pass


@dataclass(frozen=True)
class LegacyConversion:
    payload: bytes
    engine: str
    sha256: str


_WORD_SCRIPT = r'''
param([string]$Source,[string]$Target,[string]$IdentityFile)
$ErrorActionPreference='Stop'
# Never attach to or quit the customer's existing Word instance.
if (Get-Process -Name WINWORD -ErrorAction SilentlyContinue) { exit 20 }
$app=$null
$document=$null
$owned=$false
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ZskWordUser32 {
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
'@
try {
    $app=New-Object -ComObject Word.Application
    if ($app.Documents.Count -ne 0) { throw 'Word has existing documents; automatic conversion stopped' }
    [uint32]$processId=0
    [void][ZskWordUser32]::GetWindowThreadProcessId([IntPtr]$app.Hwnd,[ref]$processId)
    $process=Get-Process -Id $processId -ErrorAction Stop
    if ($process.ProcessName -ne 'WINWORD') { throw 'Word process ownership unavailable' }
    $owned=$true
    $identity=[ordered]@{pid=[int]$processId;start=$process.StartTime.ToUniversalTime().ToString('o')} | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($IdentityFile,$identity,[Text.Encoding]::UTF8)
    $app.Visible=$false
    $app.DisplayAlerts=0
    $app.AutomationSecurity=3
    $app.Options.UpdateLinksAtOpen=$false
    $document=$app.Documents.Open($Source,$false,$true,$false)
    $document.SaveAs2($Target,16)
} finally {
    if ($null -ne $document) {
        try { $document.Close(0) } catch {}
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document)
    }
    if ($null -ne $app) {
        if ($owned) { try { $app.Quit(0) } catch {} }
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($app)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
'''

_WORD_CLEANUP = r'''
param([string]$IdentityFile)
$ErrorActionPreference='Stop'
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) { exit 0 }
$identity=Get-Content -LiteralPath $IdentityFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ([int]$identity.pid -lt 1) { exit 1 }
$process=Get-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue
if ($null -eq $process -or $process.ProcessName -ne 'WINWORD') { exit 0 }
if ($process.StartTime.ToUniversalTime().ToString('o') -ne [string]$identity.start) { exit 1 }
Stop-Process -Id ([int]$identity.pid) -Force
'''


def _cleanup_word(root: Path, powershell: str) -> None:
    identity = root / "word-process.json"
    if not identity.is_file():
        return
    cleanup = root / "cleanup.ps1"
    cleanup.write_text(_WORD_CLEANUP, encoding="utf-8-sig")
    try:
        subprocess.run((powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File", str(cleanup), str(identity)),
                       capture_output=True, timeout=15, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def legacy_backend() -> tuple[str, str] | None:
    """Discover existing tools only; do not download Office or guess WPS APIs."""
    for name in ("soffice", "libreoffice"):
        executable = shutil.which(name)
        if executable:
            return "libreoffice", executable
    candidates = [Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")]
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(key):
            candidates.append(Path(os.environ[key]) / "LibreOffice/program/soffice.exe")
    for candidate in candidates:
        if candidate.is_file():
            return "libreoffice", str(candidate)
    if platform.system() == "Windows":
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if powershell:
            try:
                result = subprocess.run(
                    (powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                     "if ($null -eq [type]::GetTypeFromProgID('Word.Application')) { exit 1 }"),
                    capture_output=True, timeout=15, check=False, shell=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if result.returncode == 0:
                return "word-com", powershell
    return None


def validate_legacy_word(payload: bytes) -> None:
    if not payload.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise ConversionFailed("input is not a binary Word document; renaming is not conversion")
    try:
        import olefile
    except ImportError as exc:
        raise ConverterUnavailable("olefile is required in the ZSK host Python") from exc
    try:
        with olefile.OleFileIO(BytesIO(payload)) as document:
            if not document.exists("WordDocument") or document.exists("EncryptedPackage"):
                raise ConversionFailed("OLE input is not an unencrypted legacy Word document")
            # fEncrypted/fObfuscated in the Word FIB; don't open a password prompt.
            header = document.openstream("WordDocument").read(12)
            if len(header) < 12 or int.from_bytes(header[:2], "little") != 0xA5EC:
                raise ConversionFailed("legacy Word header is invalid")
            if int.from_bytes(header[10:12], "little") & 0x8100:
                raise ConversionFailed("encrypted legacy Word requires an unprotected original")
    except (OSError, ValueError, EOFError) as exc:
        raise ConversionFailed("legacy Word container is invalid") from exc


def convert_legacy_doc(payload: bytes) -> LegacyConversion:
    validate_legacy_word(payload)
    backend = legacy_backend()
    if backend is None:
        raise LegacyConverterUnavailable("save as DOCX with Word/WPS; no verified local converter exists")
    engine, executable = backend
    with tempfile.TemporaryDirectory(prefix="zsk-legacy-word-") as folder:
        root = Path(folder)
        source, target = root / "source.doc", root / "source.docx"
        source.write_bytes(payload)
        if engine == "libreoffice":
            profile = root / "profile"
            profile.mkdir(mode=0o700)
            # A private profile prevents interaction with an open LibreOffice session.
            (profile / "user").mkdir()
            (profile / "user/registrymodifications.xcu").write_text(
                '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                '</item></oor:items>', encoding="utf-8",
            )
            command = (executable, f"-env:UserInstallation={profile.as_uri()}", "--headless",
                       "--nologo", "--nodefault", "--nofirststartwizard", "--convert-to",
                       "docx:Office Open XML Text", "--outdir", str(root), str(source))
        else:
            script = root / "convert.ps1"
            script.write_text(_WORD_SCRIPT, encoding="utf-8-sig")
            command = (executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                       "-ExecutionPolicy", "Bypass", "-File", str(script), str(source),
                       str(target), str(root / "word-process.json"))
        try:
            result = subprocess.run(command, capture_output=True, timeout=120, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if engine == "word-com":
                _cleanup_word(root, executable)
            raise ConversionFailed("legacy Word conversion failed or timed out") from exc
        if engine == "word-com" and result.returncode == 20:
            raise LegacyConverterUnavailable("close Word before local conversion, or save as DOCX in Word/WPS")
        if result.returncode != 0 or not target.is_file() or target.is_symlink():
            if engine == "word-com":
                _cleanup_word(root, executable)
            raise ConversionFailed("legacy Word converter produced no DOCX")
        converted = target.read_bytes()
        try:
            with zipfile.ZipFile(BytesIO(converted)) as archive:
                ET.fromstring(archive.read("word/document.xml"))
                if any(name.endswith("vbaProject.bin") for name in archive.namelist()):
                    raise ConversionFailed("derived DOCX unexpectedly contains macros")
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            raise ConversionFailed("derived file is not a valid DOCX") from exc
    return LegacyConversion(converted, engine, hashlib.sha256(converted).hexdigest())
