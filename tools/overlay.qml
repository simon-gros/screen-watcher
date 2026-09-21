// Screen Watcher overlay surface (Priority 0, step 9).
//
// A wl-layer-shell OVERLAY surface. The three properties that matter:
//
//   layer: LayerOverlay          draw above normal windows, including a
//                                fullscreen game, without owning its process
//   keyboardInteractivity: None  never take focus from the game
//   mask: empty region           click-through; pointer events pass to the
//                                game underneath
//
// The empty input mask is set from Python rather than QML, because QQuickWindow
// exposes `mask` as a QRegion property that QML cannot express as empty.

import QtQuick
import QtQuick.Window
import org.kde.layershell as LayerShell

Window {
    id: root
    visible: true
    color: "transparent"
    flags: Qt.FramelessWindowHint | Qt.WindowTransparentForInput

    // Sized to content: a layer surface has no titlebar to drag, so it must
    // ask for exactly the space it needs.
    width: card.cardWidth
    height: Math.max(1, card.height)

    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.anchors: LayerShell.Window.AnchorTop | LayerShell.Window.AnchorRight
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.margins: Qt.rect(16, 16, 16, 16)
    // -1 keeps the compositor from reserving screen space for us, so the
    // overlay never shrinks a maximised game window.
    LayerShell.Window.exclusionZone: -1
    LayerShell.Window.scope: "screenwatcher"

    // Alerts are pushed in from the watcher; `detailed` toggles compact mode.
    property alias model: alerts
    property bool detailed: true

    ListModel { id: alerts }

    function pushAlert(rule, body, tone) {
        alerts.insert(0, {rule: rule, body: body, tone: tone || "normal"});
        while (alerts.count > 6) {
            alerts.remove(alerts.count - 1);
        }
        hideTimer.restart();
        root.opacity = 1.0;
    }

    // Fades out when nothing has happened, so the overlay does not sit on
    // top of the game forever after a single alert.
    Timer {
        id: hideTimer
        interval: 12000
        onTriggered: fade.start()
    }
    NumberAnimation {
        id: fade
        target: root; property: "opacity"
        to: 0.0; duration: 600
    }

    Column {
        id: card
        spacing: 6
        property int cardWidth: 380

        Repeater {
            model: alerts
            delegate: Rectangle {
                width: card.cardWidth
                height: root.detailed ? row.implicitHeight + 16 : 30
                radius: 6
                color: "#e6141414"
                border.width: 1
                border.color: model.tone === "critical" ? "#ffcc4444"
                            : model.tone === "good" ? "#ff44aa66"
                            : "#40ffffff"

                Row {
                    id: row
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 8

                    Rectangle {
                        width: 4
                        height: parent.height
                        radius: 2
                        color: model.tone === "critical" ? "#ffcc4444"
                             : model.tone === "good" ? "#ff44aa66"
                             : "#ff6688cc"
                    }
                    Column {
                        spacing: 2
                        Text {
                            text: model.rule
                            color: "#ffdddddd"
                            font.pixelSize: 11
                            font.bold: true
                        }
                        Text {
                            visible: root.detailed
                            text: model.body
                            color: "#ffffffff"
                            font.pixelSize: 13
                            width: card.cardWidth - 40
                            wrapMode: Text.WordWrap
                        }
                    }
                }
            }
        }
    }
}
