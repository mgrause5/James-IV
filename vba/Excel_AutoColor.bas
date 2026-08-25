Attribute VB_Name = "Excel_AutoColor"
Option Explicit

' =====================================================================
'  Excel_AutoColor
'  -------------------------------------------------------------------
'  Colors cell FONTS by what the cell contains, using the standard
'  financial-modelling convention:
'
'    Blue   - hardcoded inputs (numbers typed directly into the cell)
'    Black  - formulas that reference only their own sheet
'    Green  - formulas that reference another sheet in this workbook
'    Red    - formulas that link to a different workbook
'
'  Entry points:
'    AutoColorSelection    - color the currently selected cells
'    AutoColorActiveSheet  - color the whole used range of the sheet
'
'  Known limits (all rare, all fail toward a harmless color):
'    - Links to a workbook that has never been saved ([Book2]Sheet1!A1
'      has no ".xlsx" yet) are colored green, not red.
'    - A formula that reaches another sheet only through a defined name
'      cannot be detected from its text and stays black.
' =====================================================================

' ----------------------------- CONFIG --------------------------------
Private Const CLR_HARDCODE As Long = 16711680     ' RGB(0, 0, 255)  blue
Private Const CLR_FORMULA As Long = 0             ' RGB(0, 0, 0)    black
Private Const CLR_SHEET_LINK As Long = 32768      ' RGB(0, 128, 0)  green
Private Const CLR_EXTERNAL As Long = 255          ' RGB(255, 0, 0)  red

' Color text constants (labels) blue as well? Most people keep labels
' black, so this ships as False. Set True to color text inputs too.
Private Const COLOR_TEXT_CONSTANTS As Boolean = False
' ---------------------------------------------------------------------

Public Sub AutoColorSelection()
    If TypeName(Selection) <> "Range" Then
        MsgBox "Select some cells first.", vbExclamation, "AutoColor"
        Exit Sub
    End If
    AutoColorRange Selection
End Sub

Public Sub AutoColorActiveSheet()
    If ActiveSheet Is Nothing Then Exit Sub
    If TypeName(ActiveSheet) <> "Worksheet" Then
        MsgBox "The active sheet is not a worksheet.", vbExclamation, "AutoColor"
        Exit Sub
    End If
    AutoColorRange ActiveSheet.UsedRange
End Sub

Private Sub AutoColorRange(ByVal target As Range)
    Dim rng As Range
    Set rng = Intersect(target, target.Worksheet.UsedRange)
    If rng Is Nothing Then Exit Sub

    Dim oldScreen As Boolean
    oldScreen = Application.ScreenUpdating
    Application.ScreenUpdating = False
    On Error GoTo Failed

    ' SpecialCells on a one-cell range silently expands to the whole
    ' sheet, so a single cell is handled directly instead.
    If rng.Cells.CountLarge = 1 Then
        ColorOneCell rng.Cells(1, 1)
    Else
        Dim consts As Range, formulas As Range
        On Error Resume Next   ' SpecialCells raises 1004 when it finds nothing
        If COLOR_TEXT_CONSTANTS Then
            Set consts = rng.SpecialCells(xlCellTypeConstants)
        Else
            Set consts = rng.SpecialCells(xlCellTypeConstants, xlNumbers)
        End If
        Set formulas = rng.SpecialCells(xlCellTypeFormulas)
        On Error GoTo Failed

        If Not consts Is Nothing Then consts.Font.Color = CLR_HARDCODE

        If Not formulas Is Nothing Then
            Dim cell As Range
            For Each cell In formulas.Cells
                ColorFormulaCell cell
            Next cell
        End If
    End If

    Application.ScreenUpdating = oldScreen
    Exit Sub

Failed:
    Application.ScreenUpdating = oldScreen
    If Err.Number = 1004 Then
        MsgBox "Could not recolor these cells - if the sheet is protected, unprotect it and try again.", _
               vbExclamation, "AutoColor"
    Else
        MsgBox "AutoColor stopped: " & Err.Description, vbExclamation, "AutoColor"
    End If
End Sub

Private Sub ColorOneCell(ByVal cell As Range)
    If cell.HasFormula Then
        ColorFormulaCell cell
    Else
        Select Case VarType(cell.Value2)
            Case vbDouble, vbCurrency, vbLong, vbInteger, vbSingle, vbDecimal
                cell.Font.Color = CLR_HARDCODE
            Case vbString
                If COLOR_TEXT_CONSTANTS And Len(cell.Value2) > 0 Then
                    cell.Font.Color = CLR_HARDCODE
                End If
        End Select
    End If
End Sub

Private Sub ColorFormulaCell(ByVal cell As Range)
    Dim f As String
    f = StripStringLiterals(cell.Formula)
    If HasExternalRef(f) Then
        cell.Font.Color = CLR_EXTERNAL
    ElseIf HasOtherSheetRef(f, cell.Worksheet.Name) Then
        cell.Font.Color = CLR_SHEET_LINK
    Else
        cell.Font.Color = CLR_FORMULA
    End If
End Sub

' Text inside "quotes" could contain ! or [ and fool the reference
' checks, so it is removed before the formula is inspected. A doubled
' quote inside a literal ("say ""hi""") just toggles twice and stays
' removed with the rest of the literal.
Private Function StripStringLiterals(ByVal f As String) As String
    Dim i As Long, insideLiteral As Boolean, c As String, out As String
    For i = 1 To Len(f)
        c = Mid$(f, i, 1)
        If c = """" Then
            insideLiteral = Not insideLiteral
        ElseIf Not insideLiteral Then
            out = out & c
        End If
    Next i
    StripStringLiterals = out
End Function

' External links look like [Book1.xlsx]Sheet1!A1 or
' 'C:\path\[Book1.xlsx]Sheet name'!A1. Structured table references
' (Table1[Amount]) also use brackets, so a bracket pair only counts
' when its contents name a file (contain a dot) and the "[" is not
' glued onto an identifier the way a table name is.
Private Function HasExternalRef(ByVal f As String) As Boolean
    Dim openB As Long, closeB As Long
    Dim pre As String, inner As String

    openB = InStr(1, f, "[")
    Do While openB > 0
        closeB = InStr(openB + 1, f, "]")
        If closeB = 0 Then Exit Do
        inner = Mid$(f, openB + 1, closeB - openB - 1)
        If openB = 1 Then
            pre = "="
        Else
            pre = Mid$(f, openB - 1, 1)
        End If
        If InStr(1, inner, ".") > 0 And Not pre Like "[A-Za-z0-9_]" Then
            If InStr(closeB + 1, f, "!") > 0 Then
                HasExternalRef = True
                Exit Function
            End If
        End If
        openB = InStr(closeB + 1, f, "[")
    Loop
End Function

' Any "!" that survives after references to the cell's own sheet are
' removed means the formula reaches another sheet. Removing own-sheet
' references keeps =Sheet1!A1 written on Sheet1 itself black.
Private Function HasOtherSheetRef(ByVal f As String, ByVal ownSheet As String) As Boolean
    Dim s As String
    s = Replace(f, "'" & Replace(ownSheet, "'", "''") & "'!", "", 1, -1, vbTextCompare)
    s = Replace(s, ownSheet & "!", "", 1, -1, vbTextCompare)
    HasOtherSheetRef = (InStr(1, s, "!") > 0)
End Function
