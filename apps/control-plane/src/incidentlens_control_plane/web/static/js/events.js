// apps/control-plane/src/incidentlens_control_plane/web/static/js/events.js
// SSE connection for real-time updates
document.addEventListener("DOMContentLoaded", function() {
    if (typeof EventSource !== "undefined") {
        var source = new EventSource("/web/events/stream");
        source.onmessage = function(event) {
            // HTMX integration: trigger refresh on relevant elements
            var data = JSON.parse(event.data);
            if (data.type && data.type.startsWith("investigation.")) {
                htmx.trigger("body", "investigation-updated", {detail: data});
            }
        };
        source.onerror = function() {
            // Reconnect is handled by EventSource automatically
        };
    }
});
