Attribute VB_Name = "Excel_MGRibbon"
Option Explicit

' =====================================================================
'  Excel_MGRibbon
'  -------------------------------------------------------------------
'  Glue for the "MG Macros" ribbon tab and the keyboard shortcuts.
'  Import this ALONGSIDE Excel_AutoColor.bas AND Excel_MGFormat.bas
'  into the same project (ideally MGMacros.xlam - see vba/README.md),
'  then embed ribbon/Excel_MGMacros_customUI14.xml with
'  Install-MGRibbon.ps1.
'
'  Keytips (Alt, X opens the tab): D R colors the selection, S the
'  sheet; X then 1 opens the number-format chooser (1 number,
'  2 currency, 3 percent, 4 multiple); B is Copy Black.
'
'  MG_RibbonAction is the single callback every ribbon button points
'  at; it dispatches on the button id from the XML.
'
'  Shortcuts are real Ctrl-combinations bound with Application.OnKey
'  when the add-in loads (Auto_Open) and released when it closes.
'  Change them in the CONFIG block: "^" = Ctrl, "+" = Shift,
'  "%" = Alt, so "^+a" means Ctrl+Shift+A.
' =====================================================================

' ----------------------------- CONFIG --------------------------------
Private Const KEY_AUTOCOLOR_SELECTION As String = "^+a"   ' Ctrl+Shift+A
Private Const KEY_AUTOCOLOR_SHEET As String = "^+s"       ' Ctrl+Shift+S
' ---------------------------------------------------------------------

' Every button in Excel_MGMacros_customUI14.xml lands here.
Public Sub MG_RibbonAction(control As IRibbonControl)
    Select Case control.Id
        Case "mgAutoColorSel"
            AutoColorSelection
        Case "mgAutoColorSheet"
            AutoColorActiveSheet
        Case "mgFmtNum"
            FormatNumberOneDecimal
        Case "mgFmtCur"
            FormatCurrencyOneDecimal
        Case "mgFmtPct"
            FormatPercentOneDecimal
        Case "mgFmtMult"
            FormatMultipleOneDecimal
        Case "mgCopyBlack"
            CopyBlackPicture
        Case Else
            MsgBox "No macro wired up for ribbon button '" & control.Id & "'.", _
                   vbExclamation, "MG Macros"
    End Select
End Sub

' Runs automatically when the workbook/add-in holding this module
' loads, so the shortcuts are live for the whole session.
Public Sub Auto_Open()
    BindMGShortcuts
End Sub

Public Sub Auto_Close()
    UnbindMGShortcuts
End Sub

Public Sub BindMGShortcuts()
    Application.OnKey KEY_AUTOCOLOR_SELECTION, "AutoColorSelection"
    Application.OnKey KEY_AUTOCOLOR_SHEET, "AutoColorActiveSheet"
End Sub

' Restores Excel's default handling of the keys.
Public Sub UnbindMGShortcuts()
    Application.OnKey KEY_AUTOCOLOR_SELECTION
    Application.OnKey KEY_AUTOCOLOR_SHEET
End Sub
