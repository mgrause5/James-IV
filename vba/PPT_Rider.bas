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
'  deck. When the deck lives on OneDrive/SharePoint (its path is a
'  URL), the rider goes to your Documents folder instead and carries a
'  short tag derived from the deck's cloud folder, so two decks that
'  share a name get separate riders. (One caveat: the same cloud deck
'  opened via its local synced folder one day and via a web link the
'  next resolves to two different rider files.)
'
'  Each run appends the slide to the same rider, so it accumulates
'  into a scrap deck. The slide keeps its source design, master and
'  color scheme.
'
'  Safety rules: if the rider is already open with unsaved edits of
'  yours, the macro never silently saves over them - a copy is pasted
'  in and left for you to save, and a MOVE is refused outright (the
'  ripped slide must be safely on disk before it is deleted from the
'  deck). The slide is only ever deleted after a successful save.
' =====================================================================

' ----------------------------- CONFIG --------------------------------
Private Const RIDER_SUFFIX As String = " - Rider"
' ---------------------------------------------------------------------

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

    Dim riderPath As String
    riderPath = BuildRiderPath(src)

    Dim rider As Presentation
    Dim openedHere As Boolean, createdHere As Boolean
    Dim riderWasDirty As Boolean, savedOK As Boolean

    On Error GoTo Failed

    Set rider = FindOpenByFullName(riderPath)
    If rider Is Nothing Then
        If Len(Dir$(riderPath)) > 0 Then
            Set rider = Application.Presentations.Open(riderPath, WithWindow:=msoFalse)
            openedHere = True
        Else
            Set rider = Application.Presentations.Add(msoFalse)
            openedHere = True
            createdHere = True
            rider.PageSetup.SlideWidth = src.PageSetup.SlideWidth
            rider.PageSetup.SlideHeight = src.PageSetup.SlideHeight
            rider.SaveAs riderPath, ppSaveAsOpenXMLPresentation
        End If
    Else
        ' The user already has the rider open. Saving it now would also
        ' commit whatever unsaved edits they have in it - never do that
        ' silently.
        riderWasDirty = (rider.Saved = msoFalse)
        If riderWasDirty And removeFromDeck Then
            MsgBox "The rider is open with unsaved changes - save or discard them in """ & _
                   rider.Name & """ first, so the ripped slide can't end up only in an unsaved file.", _
                   vbExclamation, "Rider"
            Exit Sub
        End If
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

    Dim total As Long
    total = rider.Slides.Count

    If riderWasDirty Then
        MsgBox "Slide copied into your open rider """ & rider.Name & """." & vbCrLf & _
               "The rider has unsaved changes (yours plus this slide) - save it when you're ready.", _
               vbInformation, "Rider"
        Exit Sub
    End If

    rider.Save
    savedOK = True
    If openedHere Then rider.Close

    If removeFromDeck Then sld.Delete

    MsgBox "Slide " & IIf(removeFromDeck, "moved", "copied") & " to the rider:" & vbCrLf & _
           riderPath & vbCrLf & "(" & total & " slide(s) in the rider now.)", _
           vbInformation, "Rider"
    Exit Sub

Failed:
    Dim reason As String
    reason = Err.Description
    If savedOK Then
        ' The rider is safely on disk; only the cleanup after it failed.
        ' Retry the close so a hidden windowless rider can't outlive
        ' this run (if it already closed, the retry just no-ops).
        On Error Resume Next
        If openedHere Then rider.Close
        On Error GoTo 0
        MsgBox "The slide WAS saved to the rider, but finishing up failed: " & reason & _
               IIf(removeFromDeck, vbCrLf & "Check whether the slide still needs deleting from this deck.", ""), _
               vbExclamation, "Rider"
    Else
        On Error Resume Next
        If openedHere Then
            ' Don't leave an invisible, half-updated rider open in the
            ' session - it would hold a lock on the file and receive a
            ' duplicate slide on the next attempt.
            If Not rider Is Nothing Then
                rider.Saved = msoTrue   ' discard, so closing never prompts
                rider.Close
            End If
            ' A brand-new rider that never got its slide is just an
            ' empty stray file - remove it again.
            If createdHere Then Kill riderPath
        ElseIf Not pasted Is Nothing Then
            ' The paste landed in the user's open rider but could not
            ' be saved - take it back out, so their rider returns to
            ' its pre-macro state and a retry can't duplicate it.
            pasted.Delete
        End If
        On Error GoTo 0
        MsgBox "Could not update the rider: " & reason & vbCrLf & vbCrLf & _
               "Nothing was deleted from this deck.", vbExclamation, "Rider"
    End If
End Sub

' ----------------------------- Helpers -------------------------------

Private Function BuildRiderPath(ByVal src As Presentation) As String
    Dim folder As String, tag As String
    If InStr(1, src.Path, "://") > 0 Then
        folder = DocumentsFolder()
        tag = " (" & FolderTag(src.Path) & ")"
    Else
        folder = src.Path
        tag = ""
    End If
    BuildRiderPath = folder & "\" & BaseName(src.Name) & RIDER_SUFFIX & tag & ".pptx"
End Function

' The real Documents folder - honoring OneDrive Known Folder Move and
' corporate folder redirection - not a blind USERPROFILE\Documents.
Private Function DocumentsFolder() As String
    On Error Resume Next
    DocumentsFolder = CreateObject("WScript.Shell").SpecialFolders("MyDocuments")
    On Error GoTo 0
    If Len(DocumentsFolder) = 0 Then
        DocumentsFolder = Environ$("USERPROFILE") & "\Documents"
    End If
End Function

' Tiny stable checksum of the deck's cloud folder, rendered as 4 hex
' digits, so identically named decks in different folders do not feed
' the same rider.
Private Function FolderTag(ByVal folderUrl As String) As String
    Dim i As Long, h As Long
    For i = 1 To Len(folderUrl)
        h = (h * 31 + AscW(Mid$(folderUrl, i, 1)) And &H7FFFFFFF) Mod 65521
    Next i
    FolderTag = Right$("000" & Hex$(h), 4)
End Function

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
