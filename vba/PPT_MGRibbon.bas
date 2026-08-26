Attribute VB_Name = "PPT_MGRibbon"
Option Explicit

' =====================================================================
'  PPT_MGRibbon
'  -------------------------------------------------------------------
'  Glue for the "MG Macros" ribbon tab in PowerPoint. Import this
'  ALONGSIDE PPT_ShapeTools.bas and PPT_Rider.bas into the same
'  project (ideally MGMacros.ppam - see vba/README.md), then embed
'  ribbon/PPT_MGMacros_customUI14.xml with Install-MGRibbon.ps1.
'
'  MG_RibbonAction is the single callback every ribbon button points
'  at; it dispatches on the button id from the XML.
'
'  Keyboard access in PowerPoint: unlike Excel, PowerPoint VBA has no
'  OnKey, so there are no direct Ctrl-combinations. What you get
'  instead, and can fully customize in the XML's keytip attributes:
'
'    Alt, X, <key>      e.g.  Alt, X, S = open the Grab Size chooser,
'                             then W / H / B picks width, height, both
'                             Alt, X, A = apply what was grabbed
'                             Alt, X, T = TBU marker
'
'  For even shorter chords, add your favorite buttons to the Quick
'  Access Toolbar - QAT slots answer to Alt+1 through Alt+9.
'  (A true global Ctrl-hotkey in PowerPoint requires a Win32 keyboard
'  hook, which is fragile and often flagged by corporate security -
'  deliberately not included.)
' =====================================================================

' Every button in PPT_MGMacros_customUI14.xml lands here.
Public Sub MG_RibbonAction(control As IRibbonControl)
    Select Case control.Id
        Case "mgGrabW"
            GrabShapeWidth
        Case "mgGrabH"
            GrabShapeHeight
        Case "mgGrabB"
            GrabShapeSize
        Case "mgApply"
            ApplySize
        Case "mgApplyW"
            ApplyWidth
        Case "mgApplyH"
            ApplyHeight
        Case "mgSwap"
            SwapShapes
        Case "mgSwapTL"
            SwapShapesTopLeft
        Case "mgTBUAdd"
            InsertTBUMarker
        Case "mgTBUClearSlide"
            RemoveTBUMarkersOnSlide
        Case "mgTBUClearDeck"
            RemoveTBUMarkersInDeck
        Case "mgRiderSend"
            SendSlideToRider
        Case "mgRiderMove"
            MoveSlideToRider
        Case Else
            MsgBox "No macro wired up for ribbon button '" & control.Id & "'.", _
                   vbExclamation, "MG Macros"
    End Select
End Sub
