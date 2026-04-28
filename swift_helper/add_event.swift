#!/usr/bin/env swift
// EventKit Calendar writer for TUMsolidator.
// Reads JSON from stdin, creates an event in "TUM SoSe26" calendar,
// writes JSON result to stdout.
//
// stdin schema:
//   { "title": str, "start": ISO8601, "end": ISO8601,
//     "notes": str, "calendar": str, "external_id": str }
//
// stdout schema (success):  { "event_id": str, "created": bool }
// stdout schema (error):    { "error": str }

import EventKit
import Foundation

// MARK: — I/O types

struct EventRequest: Codable {
    let title: String
    let start: String
    let end: String
    let notes: String?
    let calendar: String?
    let external_id: String?
}

struct EventResponse: Codable {
    let event_id: String?
    let created: Bool?
    let error: String?
}

func writeResponse(_ resp: EventResponse) {
    let data = try! JSONEncoder().encode(resp)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

// MARK: — Parse ISO 8601

func parseDate(_ s: String) -> Date? {
    let fmt = ISO8601DateFormatter()
    fmt.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = fmt.date(from: s) { return d }
    fmt.formatOptions = [.withInternetDateTime]
    return fmt.date(from: s)
}

// MARK: — Main

let inputData = FileHandle.standardInput.readDataToEndOfFile()
guard let req = try? JSONDecoder().decode(EventRequest.self, from: inputData) else {
    writeResponse(EventResponse(event_id: nil, created: false, error: "Failed to parse stdin JSON"))
    exit(1)
}

guard let startDate = parseDate(req.start),
      let endDate   = parseDate(req.end) else {
    writeResponse(EventResponse(event_id: nil, created: false, error: "Invalid date format: \(req.start) / \(req.end)"))
    exit(1)
}

let store = EKEventStore()
let sema  = DispatchSemaphore(value: 0)

store.requestFullAccessToEvents { granted, error in
    defer { sema.signal() }

    guard granted, error == nil else {
        writeResponse(EventResponse(event_id: nil, created: false, error: "Calendar access denied: \(error?.localizedDescription ?? "unknown")"))
        return
    }

    let calendarName = req.calendar ?? "TUM SoSe26"

    // Find or create the target calendar
    var targetCalendar: EKCalendar? = store.calendars(for: .event).first { $0.title == calendarName }
    if targetCalendar == nil {
        let newCal       = EKCalendar(for: .event, eventStore: store)
        newCal.title     = calendarName
        // Use the default source (iCloud if available, else local)
        let preferredSources = store.sources.filter { $0.sourceType == .calDAV || $0.sourceType == .local }
        newCal.source    = preferredSources.first ?? store.defaultCalendarForNewEvents?.source
        if newCal.source != nil {
            try? store.saveCalendar(newCal, commit: true)
            targetCalendar = newCal
        }
    }

    guard let cal = targetCalendar else {
        writeResponse(EventResponse(event_id: nil, created: false, error: "Could not find or create calendar: \(calendarName)"))
        return
    }

    // Idempotency: search for existing event with matching notes/external_id
    if let extID = req.external_id {
        let pred = store.predicateForEvents(withStart: startDate - 86400, end: endDate + 86400, calendars: [cal])
        let existing = store.events(matching: pred).first { ev in
            ev.notes?.contains("external_id:\(extID)") == true
        }
        if let ev = existing {
            writeResponse(EventResponse(event_id: ev.eventIdentifier, created: false, error: nil))
            return
        }
    }

    // Create the event
    let event            = EKEvent(eventStore: store)
    event.title          = req.title
    event.startDate      = startDate
    event.endDate        = endDate
    event.calendar       = cal

    var notesLines: [String] = []
    if let n = req.notes, !n.isEmpty { notesLines.append(n) }
    if let extID = req.external_id   { notesLines.append("external_id:\(extID)") }
    event.notes = notesLines.joined(separator: "\n")

    do {
        try store.save(event, span: .thisEvent, commit: true)
        writeResponse(EventResponse(event_id: event.eventIdentifier, created: true, error: nil))
    } catch {
        writeResponse(EventResponse(event_id: nil, created: false, error: "Save failed: \(error.localizedDescription)"))
    }
}

sema.wait()
