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
'    Red    - formulas that link to a different workbook (bracketed
'             sheet links, external defined names, external tables,
'             and full-path links are all detected)
'
'  Entry points:
'    AutoColorSelection    - color the currently selected cells
'    AutoColorActiveSheet  - color the whole used range of the sheet
'
'  Re-running is safe: a blue/green/red font left behind by an earlier
'  run is reset to automatic when the cell no longer holds that kind
'  of content. Any other font color (styled labels, headers) is never
'  touched.
'
'  Known limits (rare, and each fails toward a color that still gets
'  the cell looked at):
'    - Links to a workbook that has never been saved ([Book2]Sheet1!A1
'      has no file extension yet) are colored green, not red.
'    - A formula that reaches another sheet or workbook only through a
'      defined name cannot be detected from its text and stays black.
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

        ResetStaleConventionColors rng

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

' A cell that used to be a hardcode (or link) and now holds a label or
' nothing would otherwise keep its old blue/green/red forever. Only the
' three convention colors are reset, so deliberately styled labels and
' headers keep their formatting.
Private Sub ResetStaleConventionColors(ByVal rng As Range)
    Dim stale As Range, blanks As Range
    On Error Resume Next
    If Not COLOR_TEXT_CONSTANTS Then
        Set stale = rng.SpecialCells(xlCellTypeConstants, xlTextValues + xlLogical + xlErrors)
    End If
    Set blanks = rng.SpecialCells(xlCellTypeBlanks)
    On Error GoTo 0

    If stale Is Nothing Then
        Set stale = blanks
    ElseIf Not blanks Is Nothing Then
        Set stale = Union(stale, blanks)
    End If
    If stale Is Nothing Then Exit Sub

    Dim cell As Range
    For Each cell In stale.Cells
        Select Case cell.Font.Color
            Case CLR_HARDCODE, CLR_SHEET_LINK, CLR_EXTERNAL
                cell.Font.ColorIndex = xlColorIndexAutomatic
        End Select
    Next cell
End Sub

Private Sub ColorOneCell(ByVal cell As Range)
    If cell.HasFormula Then
        ColorFormulaCell cell
    Else
        Dim isHardcode As Boolean
        Select Case VarType(cell.Value2)
            Case vbDouble, vbCurrency, vbLong, vbInteger, vbSingle, vbDecimal
                isHardcode = True
            Case vbString
                isHardcode = (COLOR_TEXT_CONSTANTS And Len(cell.Value2) > 0)
        End Select

        If isHardcode Then
            cell.Font.Color = CLR_HARDCODE
        Else
            Select Case cell.Font.Color
                Case CLR_HARDCODE, CLR_SHEET_LINK, CLR_EXTERNAL
                    cell.Font.ColorIndex = xlColorIndexAutomatic
            End Select
        End If
    End If
End Sub

Private Sub ColorFormulaCell(ByVal cell As Range)
    Dim f As String
    f = StripStringLiterals(cell.Formula)
    If HasExternalRef(f, cell.Worksheet) Then
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

Private Function HasExternalRef(ByVal f As String, ByVal ws As Worksheet) As Boolean
    ' Pass 1: a bracketed file part - [Book1.xlsx]Sheet1!A1 or
    ' 'C:\path\[Book1.xlsx]Sheet name'!A1. Structured table references
    ' (Table1[Amount], [@[Adj. EBITDA]], Table1[[#Totals],[Col]]) also
    ' use brackets, so a pair is skipped whole - including anything
    ' nested inside it - when its content starts with @, # or [ or when
    ' the "[" is glued onto an identifier (the table's name). A real
    ' file part must also be followed by "!" through nothing but a
    ' plausible sheet name, so a sheet reference elsewhere in the
    ' formula cannot turn a table reference into a false external.
    Dim openB As Long, closeB As Long
    Dim pre As String, inner As String

    openB = InStr(1, f, "[")
    Do While openB > 0
        closeB = MatchingBracket(f, openB)
        If closeB = 0 Then Exit Do
        inner = Mid$(f, openB + 1, closeB - openB - 1)
        If openB = 1 Then pre = "=" Else pre = Mid$(f, openB - 1, 1)

        If pre Like "[A-Za-z0-9_]" Or inner Like "[@#[]*" Then
            ' structured reference - skip it entirely
        ElseIf EndsWithWorkbookExt(inner) Then
            ' a genuine bracketed file part always names a workbook
            ' file; a mere dot could be a bare column ref ([Q2.Rev])
            If BangFollowsSheetName(f, closeB + 1) Then
                HasExternalRef = True
                Exit Function
            End If
        End If
        openB = InStr(closeB + 1, f, "[")
    Loop

    ' Pass 2: bracket-free external references. A link to a
    ' workbook-scoped defined name or an external table has no
    ' bracketed file part at all: =Assumptions.xlsx!WACC while the
    ' source workbook is open, ='C:\Deals\Assumptions.xlsx'!WACC once
    ' it is closed, =SUM(Book2.xlsx!Table1[#All]). The token in front
    ' of each "!" gives it away: path characters and brackets cannot
    ' appear in a sheet name, and a workbook-extension ending is
    ' external unless a local sheet really carries that name.
    Dim p As Long, j As Long, tok As String
    p = InStr(1, f, "!")
    Do While p > 0
        tok = ""
        If p > 1 Then
            If Mid$(f, p - 1, 1) = "'" Then
                j = p - 2
                Do While j >= 1          ' find the opening quote ('' = escaped)
                    If Mid$(f, j, 1) = "'" Then
                        If j = 1 Then Exit Do
                        If Mid$(f, j - 1, 1) = "'" Then j = j - 2 Else Exit Do
                    Else
                        j = j - 1
                    End If
                Loop
                If j >= 1 Then
                    tok = Mid$(f, j + 1, p - j - 2)
                    If InStr(tok, ":") > 0 Or InStr(tok, "\") > 0 Or _
                       InStr(tok, "/") > 0 Or InStr(tok, "[") > 0 Then
                        HasExternalRef = True
                        Exit Function
                    End If
                End If
            Else
                j = p - 1
                Do While j >= 1
                    If Mid$(f, j, 1) Like "[A-Za-z0-9_.]" Then j = j - 1 Else Exit Do
                Loop
                tok = Mid$(f, j + 1, p - j - 1)
            End If
        End If
        If EndsWithWorkbookExt(tok) Then
            ' a SHEET in this workbook can itself be named "Data.csv"
            ' (CSV imports); Excel resolves that reference to the local
            ' sheet, so only flag red when no such sheet exists here
            If Not IsSheetName(ws, tok) Then
                HasExternalRef = True
                Exit Function
            End If
        End If
        p = InStr(p + 1, f, "!")
    Loop
End Function

Private Function IsSheetName(ByVal ws As Worksheet, ByVal tok As String) As Boolean
    Dim sh As Object
    For Each sh In ws.Parent.Sheets
        If StrComp(sh.Name, tok, vbTextCompare) = 0 Then
            IsSheetName = True
            Exit Function
        End If
    Next sh
End Function

' Position of the "]" that truly closes the "[" at openPos, honoring
' nesting ([@[Adj. EBITDA]]), or 0 when unbalanced.
Private Function MatchingBracket(ByVal f As String, ByVal openPos As Long) As Long
    Dim i As Long, depth As Long
    For i = openPos To Len(f)
        Select Case Mid$(f, i, 1)
            Case "["
                depth = depth + 1
            Case "]"
                depth = depth - 1
                If depth = 0 Then
                    MatchingBracket = i
                    Exit Function
                End If
        End Select
    Next i
End Function

' True when, starting at startPos, the formula reads like a sheet name
' (optionally closed by ') and then a "!". Any operator, comma, or
' parenthesis on the way disqualifies the candidate.
Private Function BangFollowsSheetName(ByVal f As String, ByVal startPos As Long) As Boolean
    Dim i As Long, c As String
    For i = startPos To Len(f)
        c = Mid$(f, i, 1)
        If c = "!" Then
            BangFollowsSheetName = True
            Exit Function
        ElseIf c = "'" Then
            BangFollowsSheetName = (Mid$(f, i + 1, 1) = "!")
            Exit Function
        ElseIf c Like "[A-Za-z0-9_. -]" Then
            ' still inside a plausible sheet name - keep scanning
        ElseIf AscW(c) > 127 Or AscW(c) < 0 Then
            ' non-ASCII letters are legal in sheet names - keep scanning
        Else
            Exit Function
        End If
    Next i
End Function

Private Function EndsWithWorkbookExt(ByVal tok As String) As Boolean
    tok = LCase$(tok)
    EndsWithWorkbookExt = tok Like "*.xls" Or tok Like "*.xls?" _
        Or tok Like "*.xlt" Or tok Like "*.xlt?" Or tok Like "*.xla" _
        Or tok Like "*.xlam" Or tok Like "*.csv" Or tok Like "*.ods"
End Function

' Any "!" that survives after references to the cell's own sheet are
' removed means the formula reaches another sheet. Removal is token-
' boundary aware, so on sheet "Model" the reference DCF_Model!C5 keeps
' its "!" (only a leading match of "Model!" is the own sheet), while
' =Model!A1 on Model itself still comes out black.
Private Function HasOtherSheetRef(ByVal f As String, ByVal ownSheet As String) As Boolean
    Dim s As String
    s = RemoveOwnSheetToken(f, "'" & Replace(ownSheet, "'", "''") & "'!")
    s = RemoveOwnSheetToken(s, ownSheet & "!")
    HasOtherSheetRef = (InStr(1, s, "!") > 0)
End Function

' Removes tok wherever it starts at a token boundary: the character
' before the match may not be an identifier character (which would make
' the match the TAIL of a longer sheet name, e.g. Model! inside
' DCF_Model!) or a quote (the tail of a longer quoted name).
Private Function RemoveOwnSheetToken(ByVal s As String, ByVal tok As String) As String
    Dim p As Long, startPos As Long, kept As String, prevC As String
    startPos = 1
    p = InStr(startPos, s, tok, vbTextCompare)
    Do While p > 0
        If p = 1 Then prevC = "" Else prevC = Mid$(s, p - 1, 1)
        If Len(prevC) = 0 Or Not prevC Like "[A-Za-z0-9_.']" Then
            kept = kept & Mid$(s, startPos, p - startPos)
        Else
            kept = kept & Mid$(s, startPos, p - startPos + Len(tok))
        End If
        startPos = p + Len(tok)
        p = InStr(startPos, s, tok, vbTextCompare)
    Loop
    RemoveOwnSheetToken = kept & Mid$(s, startPos)
End Function
