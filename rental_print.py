"""Print rendered form pages through the Windows printer driver."""

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf as pdf


PRINT_PAGES = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Drawing
$pages = @(Get-ChildItem -LiteralPath (Get-Location).Path -Filter 'page-*.png' -File |
    Sort-Object Name)
if ($pages.Count -eq 0) { throw "Het formulier bevat geen afdrukbare pagina's." }
$position = @{ Index = 0 }
$document = New-Object System.Drawing.Printing.PrintDocument
try {
    if (-not $document.PrinterSettings.IsValid) {
        throw 'Windows heeft geen beschikbare standaardprinter.'
    }
    $document.DocumentName = 'Onderdelenformulier'
    $document.PrintController = New-Object System.Drawing.Printing.StandardPrintController
    $document.DefaultPageSettings.Landscape = $false
    $paper = $document.PrinterSettings.PaperSizes |
        Where-Object { $_.Kind -eq [System.Drawing.Printing.PaperKind]::A4 } |
        Select-Object -First 1
    if ($null -eq $paper) { throw 'De standaardprinter biedt geen A4-papierformaat aan.' }
    $document.DefaultPageSettings.PaperSize = $paper
    $document.add_PrintPage({
        param($sender, $printPage)
        $image = [System.Drawing.Image]::FromFile($pages[$position.Index].FullName)
        try {
            $area = $printPage.PageSettings.PrintableArea
            $scale = [Math]::Min($area.Width / $image.Width, $area.Height / $image.Height)
            $width = $image.Width * $scale
            $height = $image.Height * $scale
            $rectangle = [System.Drawing.RectangleF]::new(
                [single](($area.Width - $width) / 2), [single](($area.Height - $height) / 2),
                [single]$width, [single]$height)
            $printPage.Graphics.DrawImage($image, $rectangle)
        } finally {
            $image.Dispose()
        }
        $position.Index++
        $printPage.HasMorePages = $position.Index -lt $pages.Count
    })
    $document.Print()
    [Console]::WriteLine($document.PrinterSettings.PrinterName)
} finally {
    $document.Dispose()
}
"""


def print_rental_pdf(path):
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    with TemporaryDirectory(prefix="lego-afdruk-") as folder, pdf.open(path) as document:
        if not len(document):
            raise ValueError("Het formulier bevat geen pagina’s.")
        for index, page in enumerate(document, 1):
            page.get_pixmap(dpi=200, alpha=False, annots=True).save(Path(folder) / f"page-{index:06d}.png")
        try:
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", PRINT_PAGES],
                cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("De Windows-afdrukservice reageert niet. Controleer de afdrukwachtrij voordat je opnieuw afdrukt.") from exc
        if result.returncode:
            detail = result.stderr.strip().splitlines()
            raise RuntimeError(detail[0] if detail else "Windows kon de afdrukopdracht niet verwerken.")
        return result.stdout.strip()
