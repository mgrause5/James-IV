Attribute VB_Name = "PPT_ShapeTools"
Option Explicit

' =====================================================================
'  PPT_ShapeTools
'  -------------------------------------------------------------------
'  Size tools (a format-painter for dimensions):
'    GrabShapeSize   - remember the selected shape's width and height
'    ApplyWidth      - give the remembered width to the selected shapes
'    ApplyHeight     - give the remembered height to the selected shapes
'    ApplySize       - give both dimensions to the selected shapes
'
'  Applying a size turns OFF "resize shape to fit text" (autofit) on
'  the target shapes - autofit would silently snap them straight back
'  to hugging their text. Aspect-ratio lock is parked and restored.
'
'  Layout tools:
'    SwapShapes         - two selected shapes trade places (their
'                         centers swap, so different-sized shapes stay
'                         visually balanced in each other's spot)
'    SwapShapesTopLeft  - same, but the top-left corners swap instead
'
'  TBU tools:
'    InsertTBUMarker         - drop a pre-formatted "TBU" flag, pinned
'                              to the selected shape's top-right corner
'                              (or the slide's, with nothing selected)
'    RemoveTBUMarkersOnSlide - clear every marker on the current slide
'    RemoveTBUMarkersInDeck  - clear every marker in the presentation
'
'  Markers are tracked by an internal tag, and removal looks inside
'  groups too - a marker grouped with the shape it flags still gets
'  cleaned up.
'
'  The grabbed size lives until PowerPoint closes or the VBA project
'  resets, and works across presentations. Grabbing is silent by
'  design (no popup between grab and apply); flip CONFIRM_GRAB below
'  if you want a confirmation.
' =====================================================================

' ----------------------------- CONFIG --------------------------------
Private Const TBU_TEXT As String = "TBU"
Private Const TBU_FONT As String = "Arial"
Private Const TBU_FONT_SIZE As Single = 12
Private Const TBU_BOX_W As Single = 42            ' points
Private Const TBU_BOX_H As Single = 20            ' points
Private Const TBU_FILL As Long = 65535            ' RGB(255, 255, 0) yellow
Private Const TBU_TEXT_COLOR As Long = 192        ' RGB(192, 0, 0)   dark red
Private Const TBU_LINE_COLOR As Long = 192        ' RGB(192, 0, 0)   dark red

Private Const CONFIRM_GRAB As Boolean = False     ' popup after each grab?
' ---------------------------------------------------------------------

Private mGrabbedW As Single
Private mGrabbedH As Single
Private mHaveSize As Boolean

' ------------------------------ Size ---------------------------------

Public Sub GrabShapeSize()
    Dim shps As ShapeRange
    Set shps = SelectedShapes()
    If shps Is Nothing Then
        MsgBox "Select the shape whose size you want to grab.", vbExclamation, "Grab size"
        Exit Sub
    End If
    If shps.Count <> 1 Then
        MsgBox "Select exactly one shape to grab from (you have " & shps.Count & " selected).", _
               vbExclamation, "Grab size"
        Exit Sub
    End If

    mGrabbedW = shps(1).Width
    mGrabbedH = shps(1).Height
    mHaveSize = True

    If CONFIRM_GRAB Then
        MsgBox "Grabbed " & Format$(mGrabbedW / 72, "0.00") & """ wide x " & _
               Format$(mGrabbedH / 72, "0.00") & """ tall.", vbInformation, "Grab size"
    End If
End Sub

Public Sub ApplyWidth()
    ApplyGrabbed True, False
End Sub

Public Sub ApplyHeight()
    ApplyGrabbed False, True
End Sub

Public Sub ApplySize()
    ApplyGrabbed True, True
End Sub

Private Sub ApplyGrabbed(ByVal doWidth As Boolean, ByVal doHeight As Boolean)
    If Not mHaveSize Then
        MsgBox "Nothing grabbed yet - run GrabShapeSize on a shape first.", vbExclamation, "Apply size"
        Exit Sub
    End If

    Dim shps As ShapeRange
    Set shps = SelectedShapes()
    If shps Is Nothing Then
        MsgBox "Select the shape(s) you want to resize.", vbExclamation, "Apply size"
        Exit Sub
    End If

    Dim shp As Shape, lockState As MsoTriState
    For Each shp In shps
        ' LockAspectRatio would drag the other dimension along, so it
        ' is parked off while the new size goes in, then restored.
        lockState = shp.LockAspectRatio
        shp.LockAspectRatio = msoFalse

        ' Autofit ("resize shape to fit text", the default on inserted
        ' text boxes) recomputes the size from the text the moment it
        ' changes, silently undoing the apply - so it stays off.
        On Error Resume Next   ' lines, pictures, groups: no text frame
        If shp.HasTextFrame Then
            If shp.TextFrame.AutoSize <> ppAutoSizeNone Then
                shp.TextFrame.AutoSize = ppAutoSizeNone
            End If
        End If
        On Error GoTo 0

        If doWidth Then shp.Width = mGrabbedW
        If doHeight Then shp.Height = mGrabbedH
        shp.LockAspectRatio = lockState
    Next shp
End Sub

' ------------------------------ Swap ---------------------------------

Public Sub SwapShapes()
    SwapSelectedPair True
End Sub

Public Sub SwapShapesTopLeft()
    SwapSelectedPair False
End Sub

Private Sub SwapSelectedPair(ByVal byCenter As Boolean)
    Dim shps As ShapeRange
    Set shps = SelectedShapes()
    If shps Is Nothing Then
        MsgBox "Select the two shapes you want to swap.", vbExclamation, "Swap shapes"
        Exit Sub
    End If
    If shps.Count <> 2 Then
        MsgBox "Select exactly two shapes to swap (you have " & shps.Count & " selected).", _
               vbExclamation, "Swap shapes"
        Exit Sub
    End If

    Dim a As Shape, b As Shape
    Set a = shps(1)
    Set b = shps(2)

    If byCenter Then
        Dim aCx As Single, aCy As Single, bCx As Single, bCy As Single
        aCx = a.Left + a.Width / 2
        aCy = a.Top + a.Height / 2
        bCx = b.Left + b.Width / 2
        bCy = b.Top + b.Height / 2
        a.Left = bCx - a.Width / 2
        a.Top = bCy - a.Height / 2
        b.Left = aCx - b.Width / 2
        b.Top = aCy - b.Height / 2
    Else
        Dim aL As Single, aT As Single
        aL = a.Left
        aT = a.Top
        a.Left = b.Left
        a.Top = b.Top
        b.Left = aL
        b.Top = aT
    End If
End Sub

' ------------------------------- TBU ---------------------------------

Public Sub InsertTBUMarker()
    Dim sld As Slide
    Set sld = CurrentSlide()
    If sld Is Nothing Then
        MsgBox "Go to the slide (Normal view) where you want the marker.", vbExclamation, "TBU"
        Exit Sub
    End If

    ' Pin the marker to the top-right corner of the selected shape, or
    ' of the slide when nothing is selected. Repeated markers cascade a
    ' few points so they never stack invisibly on top of each other.
    Dim x As Single, y As Single
    Dim shps As ShapeRange
    Set shps = SelectedShapes()
    If Not shps Is Nothing Then
        x = shps(1).Left + shps(1).Width - TBU_BOX_W / 2
        y = shps(1).Top - TBU_BOX_H / 2
    Else
        x = sld.Parent.PageSetup.SlideWidth - TBU_BOX_W - 12
        y = 12
    End If

    Dim offset As Single
    offset = 6 * CountTBUMarkers(sld)
    x = x - offset
    y = y + offset

    If x < 0 Then x = 0
    If y < 0 Then y = 0
    If x + TBU_BOX_W > sld.Parent.PageSetup.SlideWidth Then
        x = sld.Parent.PageSetup.SlideWidth - TBU_BOX_W
    End If

    Dim shp As Shape
    Set shp = sld.Shapes.AddTextbox(msoTextOrientationHorizontal, x, y, TBU_BOX_W, TBU_BOX_H)
    With shp
        .Name = "TBU_marker_" & Format$(Now, "hhnnss")
        .Tags.Add "TBU_MARKER", "1"
        With .TextFrame
            .WordWrap = msoFalse
            .AutoSize = ppAutoSizeNone
            .MarginLeft = 2
            .MarginRight = 2
            .MarginTop = 1
            .MarginBottom = 1
            With .TextRange
                .Text = TBU_TEXT
                .ParagraphFormat.Alignment = ppAlignCenter
                With .Font
                    .Name = TBU_FONT
                    .Size = TBU_FONT_SIZE
                    .Bold = msoTrue
                    .Color.RGB = TBU_TEXT_COLOR
                End With
            End With
        End With
        .Fill.Visible = msoTrue
        .Fill.Solid
        .Fill.ForeColor.RGB = TBU_FILL
        .Line.Visible = msoTrue
        .Line.ForeColor.RGB = TBU_LINE_COLOR
        .Line.Weight = 1
    End With
End Sub

Public Sub RemoveTBUMarkersOnSlide()
    Dim sld As Slide
    Set sld = CurrentSlide()
    If sld Is Nothing Then
        MsgBox "Go to the slide (Normal view) you want to clean up.", vbExclamation, "TBU"
        Exit Sub
    End If
    Dim removed As Long
    removed = RemoveTBUFromSlide(sld)
    MsgBox removed & " TBU marker(s) removed from this slide.", vbInformation, "TBU"
End Sub

Public Sub RemoveTBUMarkersInDeck()
    If Application.Presentations.Count = 0 Then
        MsgBox "Open a presentation first.", vbExclamation, "TBU"
        Exit Sub
    End If
    Dim sld As Slide, removed As Long
    For Each sld In ActivePresentation.Slides
        removed = removed + RemoveTBUFromSlide(sld)
    Next sld
    MsgBox removed & " TBU marker(s) removed from the deck.", vbInformation, "TBU"
End Sub

' Two-phase removal: first collect Shape references (recursing into
' groups so a marker grouped with the shape it flags is still found),
' then delete them. Deleting while iterating would fight the
' collections' re-indexing - and a group dropping to one member can
' dissolve, invalidating the very GroupItems being walked. A delete
' can also orphan a SIBLING reference the same way (two markers
' grouped together), so each round tolerates dead references and the
' slide is re-collected until a sweep finds nothing left.
Private Function RemoveTBUFromSlide(ByVal sld As Slide) As Long
    Dim total As Long, rounds As Long
    Dim doomed As Collection, shp As Shape, deleted As Long

    Do
        Set doomed = New Collection
        CollectTBUMarkers sld.Shapes, doomed
        If doomed.Count = 0 Then Exit Do

        deleted = 0
        For Each shp In doomed
            On Error Resume Next
            shp.Delete
            If Err.Number = 0 Then deleted = deleted + 1
            Err.Clear
            On Error GoTo 0
        Next shp
        total = total + deleted
        If deleted = 0 Then Exit Do   ' nothing deletable - don't spin
        rounds = rounds + 1
    Loop While rounds < 10

    RemoveTBUFromSlide = total
End Function

' shps is a Shapes or GroupShapes collection.
Private Sub CollectTBUMarkers(ByVal shps As Object, ByVal doomed As Collection)
    Dim i As Long
    For i = 1 To shps.Count
        If shps(i).Tags("TBU_MARKER") <> "" Then
            doomed.Add shps(i)
        ElseIf shps(i).Type = msoGroup Then
            CollectTBUMarkers shps(i).GroupItems, doomed
        End If
    Next i
End Sub

Private Function CountTBUMarkers(ByVal sld As Slide) As Long
    CountTBUMarkers = CountTBUInCollection(sld.Shapes)
End Function

Private Function CountTBUInCollection(ByVal shps As Object) As Long
    Dim i As Long, n As Long
    For i = 1 To shps.Count
        If shps(i).Tags("TBU_MARKER") <> "" Then
            n = n + 1
        ElseIf shps(i).Type = msoGroup Then
            n = n + CountTBUInCollection(shps(i).GroupItems)
        End If
    Next i
    CountTBUInCollection = n
End Function

' ----------------------------- Helpers -------------------------------

Private Function SelectedShapes() As ShapeRange
    Dim sel As Selection
    On Error Resume Next
    Set sel = ActiveWindow.Selection
    On Error GoTo 0
    If sel Is Nothing Then Exit Function

    Select Case sel.Type
        Case ppSelectionShapes, ppSelectionText
            On Error Resume Next
            Set SelectedShapes = sel.ShapeRange
            On Error GoTo 0
    End Select
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
