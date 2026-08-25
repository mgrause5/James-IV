Attribute VB_Name = "PPT_Rider"
Option Explicit

' =====================================================================
'  PPT_Rider
'  -------------------------------------------------------------------
'  Rips the slide you are currently on into a rider file so you can
'  section out drafts and template changes without losing them.
'
'    SendSlideToRider - COPY the current slide into the rider
'    MoveSlideToRider - copy it, then DELETE it from this deck
'
'  The rider is "<deck name> - Rider.pptx" in the same folder as the
'  deck (or in your Documents folder when the deck lives on
'  OneDrive/SharePoint, where the path is a URL). Each run appends the
'  slide to the same rider, so it accumulates into a scrap deck. The
'  slide keeps its source design, master and color scheme.
' =====================================================================

Private Const RIDER_SUFFIX As String = " - Rider"

Public Sub SendSlideToRider()
    CopySlideToRider False
End Sub

Public Sub MoveSlideToRider()
    CopySlideToRider True
End Sub

Private Sub CopySlideToRider(ByVal removeFromDeck As Boolean)
    Dim src As Presentation
    On Error Resume Next
    Set src = ActivePresentation
    On Error GoTo 0
    If src Is Nothing Then
        MsgBox "Open a presentation first.", vbExclamation, "Rider"
        Exit Sub
    End If
    If Len(src.Path) = 0 Then
        MsgBox "Save this presentation first - the rider file is named after it.", _
               vbExclamation, "Rider"
        Exit Sub
    End If

    Dim sld As Slide
    Set sld = CurrentSlide()
    If sld Is Nothing Then
        MsgBox "Go to the slide (Normal view) you want to rip into the rider.", _
               vbExclamation, "Rider"
        Exit Sub
    End If

    ' A deck on OneDrive/SharePoint reports an https:// path that local
    ' file checks cannot touch, so the rider goes to Documents instead.
    Dim folder As String
    If InStr(1, src.Path, "://") > 0 Then
        folder = Environ$("USERPROFILE") & "\Documents"
    Else
        folder = src.Path
    End If

    Dim riderPath As String
    riderPath = folder & "\" & BaseName(src.Name) & RIDER_SUFFIX & ".pptx"

    On Error GoTo Failed

    Dim rider As Presentation, openedHere As Boolean
    Set rider = FindOpenByFullName(riderPath)
    If rider Is Nothing Then
        If Len(Dir$(riderPath)) > 0 Then
            Set rider = Application.Presentations.Open(riderPath, WithWindow:=msoFalse)
        Else
            Set rider = Application.Presentations.Add(msoFalse)
            rider.PageSetup.SlideWidth = src.PageSetup.SlideWidth
            rider.PageSetup.SlideHeight = src.PageSetup.SlideHeight
            rider.SaveAs riderPath, ppSaveAsOpenXMLPresentation
        End If
        openedHere = True
    End If

    sld.Copy
    DoEvents

    Dim pasted As Slide
    Set pasted = rider.Slides.Paste(rider.Slides.Count + 1)(1)

    ' Pasting alone would re-theme the slide to the rider's design;
    ' carrying the source design and color scheme over keeps it looking
    ' exactly as it did in the deck.
    On Error Resume Next
    pasted.Design = sld.Design
    pasted.ColorScheme = sld.ColorScheme
    If sld.FollowMasterBackground = msoFalse Then
        pasted.FollowMasterBackground = msoFalse
    End If
    On Error GoTo Failed

    rider.Save
    Dim total As Long
    total = rider.Slides.Count
    If openedHere Then rider.Close

    If removeFromDeck Then sld.Delete

    MsgBox "Slide " & IIf(removeFromDeck, "moved", "copied") & " to the rider:" & vbCrLf & _
           riderPath & vbCrLf & "(" & total & " slide(s) in the rider now.)", _
           vbInformation, "Rider"
    Exit Sub

Failed:
    MsgBox "Could not update the rider: " & Err.Description & vbCrLf & vbCrLf & _
           "Nothing was deleted from this deck.", vbExclamation, "Rider"
End Sub

' ----------------------------- Helpers -------------------------------

Private Function FindOpenByFullName(ByVal fullPath As String) As Presentation
    Dim p As Presentation
    For Each p In Application.Presentations
        If StrComp(p.FullName, fullPath, vbTextCompare) = 0 Then
            Set FindOpenByFullName = p
            Exit Function
        End If
    Next p
End Function

Private Function BaseName(ByVal fileName As String) As String
    Dim dotPos As Long
    dotPos = InStrRev(fileName, ".")
    If dotPos > 0 Then
        BaseName = Left$(fileName, dotPos - 1)
    Else
        BaseName = fileName
    End If
End Function

Private Function CurrentSlide() As Slide
    On Error Resume Next
    Set CurrentSlide = ActiveWindow.View.Slide
    If CurrentSlide Is Nothing Then
        If ActiveWindow.Selection.Type = ppSelectionSlides Then
            Set CurrentSlide = ActiveWindow.Selection.SlideRange(1)
        End If
    End If
    On Error GoTo 0
End Function
