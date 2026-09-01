Attribute VB_Name = "Excel_MGFormat"
Option Explicit

' =====================================================================
'  Excel_MGFormat
'  -------------------------------------------------------------------
'  Number formats that stay decimal-aligned when mixed in one column,
'  quick shading, and Copy Black for external outputs. Wired to the
'  MG Macros tab via Excel_MGRibbon.bas: Format menu (Alt, X, F, 1,
'  then 1-4), fills (Alt, X, B / G), outline (Alt, X, X), Copy Black
'  (Alt, X, C).
'
'    FormatNumberOneDecimal     100.1
'    FormatCurrencyOneDecimal   $100.1
'    FormatPercentOneDecimal    12.3%
'    FormatMultipleOneDecimal   2.4x
'
'  Alignment trick: every format reserves the same trailing width -
'  the actual suffix it shows plus invisible padding the exact width
'  of the suffixes it doesn't ("_x" pads the width of an x, "_%" the
'  width of a %). So 100.1 / $8.2 / 12.3% / 2.4x all line up
'  digit-for-digit at the decimal, suffixes hanging off the right.
'
'    FillSelectionLightBlue  - light blue fill on the selected cells
'    FillSelectionLightGrey  - light grey fill on the selected cells
'    OutlineSelectionGrey    - thin medium-grey box around the selection
'
'    CopyBlackPicture - copies the selected range to the clipboard as
'    a picture with ALL text black, so a green/blue-coded sheet can go
'    into an external page without shipping the color code. The
'    original sheet is untouched (the recolor happens on a throwaway
'    copy). Cell contents only - embedded charts aren't included, and
'    colors applied by conditional formatting stay as they are.
' =====================================================================

' ----------------------------- CONFIG --------------------------------
Private Const FMT_NUMBER As String = "#,##0.0_x_%"
Private Const FMT_CURRENCY As String = "$#,##0.0_x_%"
Private Const FMT_PERCENT As String = "#,##0.0%_x"
Private Const FMT_MULTIPLE As String = "#,##0.0""x""_%"

Private Const FILL_LIGHT_BLUE As Long = 16247773   ' RGB(221, 235, 247)
Private Const FILL_LIGHT_GREY As Long = 14277081   ' RGB(217, 217, 217)
Private Const LINE_MEDIUM_GREY As Long = 10921638  ' RGB(166, 166, 166)
' ---------------------------------------------------------------------

Public Sub FormatNumberOneDecimal()
    ApplyNumberFormat FMT_NUMBER
End Sub

Public Sub FormatCurrencyOneDecimal()
    ApplyNumberFormat FMT_CURRENCY
End Sub

Public Sub FormatPercentOneDecimal()
    ApplyNumberFormat FMT_PERCENT
End Sub

Public Sub FormatMultipleOneDecimal()
    ApplyNumberFormat FMT_MULTIPLE
End Sub

Private Sub ApplyNumberFormat(ByVal fmt As String)
    If TypeName(Selection) <> "Range" Then
        MsgBox "Select some cells first.", vbExclamation, "MG Format"
        Exit Sub
    End If
    On Error GoTo Failed
    Selection.NumberFormat = fmt
    Exit Sub
Failed:
    MsgBox "Could not apply the format - if the sheet is protected, unprotect it and try again.", _
           vbExclamation, "MG Format"
End Sub

Public Sub FillSelectionLightBlue()
    ApplyFill FILL_LIGHT_BLUE
End Sub

Public Sub FillSelectionLightGrey()
    ApplyFill FILL_LIGHT_GREY
End Sub

Private Sub ApplyFill(ByVal fillColor As Long)
    If TypeName(Selection) <> "Range" Then
        MsgBox "Select some cells first.", vbExclamation, "MG Format"
        Exit Sub
    End If
    On Error GoTo Failed
    With Selection.Interior
        .Pattern = xlSolid
        .Color = fillColor
    End With
    Exit Sub
Failed:
    MsgBox "Could not fill the cells - if the sheet is protected, unprotect it and try again.", _
           vbExclamation, "MG Format"
End Sub

' A thin medium-grey border around the OUTSIDE of the selection (each
' area gets its own box when several are selected).
Public Sub OutlineSelectionGrey()
    If TypeName(Selection) <> "Range" Then
        MsgBox "Select the range you want boxed.", vbExclamation, "MG Format"
        Exit Sub
    End If
    On Error GoTo Failed
    Dim area As Range
    For Each area In Selection.Areas
        area.BorderAround LineStyle:=xlContinuous, Weight:=xlThin, Color:=LINE_MEDIUM_GREY
    Next area
    Exit Sub
Failed:
    MsgBox "Could not draw the box - if the sheet is protected, unprotect it and try again.", _
           vbExclamation, "MG Format"
End Sub

' Copies the selection as a picture whose text is entirely black. The
' selection is duplicated onto a throwaway sheet (values, formats,
' column widths, row heights), recolored there, photographed, and the
' throwaway sheet deleted - the real sheet is never modified.
Public Sub CopyBlackPicture()
    If TypeName(Selection) <> "Range" Then
        MsgBox "Select the range you want to copy.", vbExclamation, "Copy Black"
        Exit Sub
    End If
    Dim src As Range
    Set src = Selection
    If src.Areas.Count > 1 Then
        MsgBox "Select one contiguous range - a multi-area selection can't be copied as a picture.", _
               vbExclamation, "Copy Black"
        Exit Sub
    End If

    Dim srcSheet As Worksheet, tmp As Worksheet
    Set srcSheet = src.Worksheet

    ' A column-header click or Ctrl+A selects a million rows; clamp to
    ' the cells actually in use so the copy stays instant.
    Set src = Intersect(src, srcSheet.UsedRange)
    If src Is Nothing Then
        MsgBox "The selection has no used cells to copy.", vbExclamation, "Copy Black"
        Exit Sub
    End If
    If src.Cells.CountLarge > 100000 Then
        MsgBox "That's " & Format$(src.Cells.CountLarge, "#,##0") & _
               " cells - too big to photograph sensibly. Select the actual exhibit range.", _
               vbExclamation, "Copy Black"
        Exit Sub
    End If

    Dim showGrid As Boolean
    showGrid = ActiveWindow.DisplayGridlines

    Dim oldScreen As Boolean
    oldScreen = Application.ScreenUpdating
    Application.ScreenUpdating = False
    Application.DisplayAlerts = False
    On Error GoTo Failed

    Set tmp = srcSheet.Parent.Worksheets.Add(After:=srcSheet.Parent.Sheets(srcSheet.Parent.Sheets.Count))

    src.Copy
    With tmp.Range("A1")
        .PasteSpecial xlPasteColumnWidths
        .PasteSpecial xlPasteValuesAndNumberFormats
        .PasteSpecial xlPasteFormats
    End With
    Application.CutCopyMode = False

    Dim pasted As Range
    Set pasted = tmp.Range("A1").Resize(src.Rows.Count, src.Columns.Count)

    ' PasteSpecial carries column widths but not row heights
    Dim r As Long
    For r = 1 To src.Rows.Count
        tmp.Rows(r).RowHeight = src.Rows(r).RowHeight
    Next r

    pasted.Font.Color = vbBlack

    tmp.Activate
    ActiveWindow.DisplayGridlines = showGrid
    pasted.CopyPicture Appearance:=xlScreen, Format:=xlPicture

    srcSheet.Activate
    tmp.Delete
    Set tmp = Nothing

    Application.DisplayAlerts = True
    Application.ScreenUpdating = oldScreen
    MsgBox "A black-text picture of the selection is on the clipboard - paste it with Ctrl+V.", _
           vbInformation, "Copy Black"
    Exit Sub

Failed:
    Dim reason As String
    reason = Err.Description
    On Error Resume Next
    If Not tmp Is Nothing Then
        srcSheet.Activate
        tmp.Delete
    End If
    On Error GoTo 0
    Application.DisplayAlerts = True
    Application.ScreenUpdating = oldScreen
    MsgBox "Copy Black failed: " & reason, vbExclamation, "Copy Black"
End Sub
