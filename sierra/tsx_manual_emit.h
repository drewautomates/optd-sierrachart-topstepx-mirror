// tsx_manual_emit.h - TopstepX manual-trade mirror emitter (header-only, ACSIL)
// Copyright (c) 2026 Andrew Thomas (OPTD, onepersontradedesk.com)
// MIT License - see LICENSE, which also carries the risk disclosure.
// This code places real orders on a real account. You are responsible
// for every order it sends.
//
// Used by Manual_Mirror.cpp, which watches the order list of the selected
// trade account and appends one JSON line per order state transition to a
// daily outbox file. The companion Python service (bridge/manual_bridge.py)
// tails that file and turns each line into a TopstepX REST call.
//
// Output path: <outbox_dir>\YYYYMMDD.jsonl   (UTC date)
//   outbox_dir comes from the study's "Outbox Directory" input and must be
//   the same folder as paths.outbox in the bridge's manual_config.yaml.
//
// Tag format: "manual-{sc_internal_id}-v{N}" where N increments on each
// cancel_replace. Cancels use "manual-{sc_internal_id}-cancel". Deterministic
// per Sierra Chart InternalOrderID, so replay is safe (the bridge dedupes on
// tag). Tags are LOCAL bookkeeping only - the bridge never sends them to
// TopstepX.

#ifndef TSX_MANUAL_EMIT_H
#define TSX_MANUAL_EMIT_H

#include "sierrachart.h"

#include <chrono>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <sstream>
#include <string>

namespace tsx_manual {

inline std::string TodayUtc()
{
    std::time_t now = std::time(nullptr);
    std::tm gmt{};
#if defined(_WIN32)
    gmtime_s(&gmt, &now);
#else
    gmtime_r(&now, &gmt);
#endif
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%04d%02d%02d",
                  gmt.tm_year + 1900, gmt.tm_mon + 1, gmt.tm_mday);
    return std::string(buf);
}

inline std::string NowIsoUtc()
{
    auto now = std::chrono::system_clock::now();
    auto epoch_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()).count();
    std::time_t sec = (std::time_t)(epoch_ms / 1000);
    int ms = (int)(epoch_ms % 1000);
    std::tm gmt{};
#if defined(_WIN32)
    gmtime_s(&gmt, &sec);
#else
    gmtime_r(&sec, &gmt);
#endif
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ",
                  gmt.tm_year + 1900, gmt.tm_mon + 1, gmt.tm_mday,
                  gmt.tm_hour, gmt.tm_min, gmt.tm_sec, ms);
    return std::string(buf);
}

inline std::string JsonEscape(const std::string& s)
{
    std::string out;
    out.reserve(s.size() + 4);
    for (char c : s) {
        if (c == '\\' || c == '"') out.push_back('\\');
        out.push_back(c);
    }
    return out;
}

// Append one line to today's outbox file, creating the leaf directory if
// needed. Fails loud (message log, error level) rather than silently dropping
// a command.
inline void AppendLine(SCStudyInterfaceRef sc, const char* outbox_dir,
                       const std::string& line)
{
    std::string outbox(outbox_dir ? outbox_dir : "");
    if (outbox.empty()) {
        sc.AddMessageToLog("TSX manual emit: Outbox Directory is empty - line dropped", 1);
        return;
    }
    CreateDirectoryA(outbox.c_str(), NULL);
    const char last = outbox[outbox.size() - 1];
    if (last != '\\' && last != '/') outbox += "\\";
    const std::string path = outbox + TodayUtc() + ".jsonl";
    std::ofstream f(path, std::ios::app | std::ios::binary);
    if (!f.is_open()) {
        SCString msg;
        msg.Format("TSX manual emit: failed to open %s", path.c_str());
        sc.AddMessageToLog(msg, 1);
        return;
    }
    f << line << "\n";
    f.flush();
}

inline std::string MakeTag(const std::string& sc_internal_id, int version)
{
    std::ostringstream oss;
    oss << "manual-" << sc_internal_id << "-v" << version;
    return oss.str();
}

// Market entry. Used when the study sees a new order already in Open/Filled
// state that is a Market type, or when cancel_replace swaps a working order
// to a market.
inline void EmitPlaceMarket(SCStudyInterfaceRef sc, const char* outbox_dir,
                            const std::string& sc_internal_id, int version,
                            const char* contract_key, bool is_long, int size)
{
    const std::string tag = MakeTag(sc_internal_id, version);
    std::ostringstream oss;
    oss << "{"
        << "\"ts\":\"" << NowIsoUtc() << "\","
        << "\"cmd\":\"place_market\","
        << "\"tag\":\"" << JsonEscape(tag) << "\","
        << "\"sc_id\":\"" << JsonEscape(sc_internal_id) << "\","
        << "\"version\":" << version << ","
        << "\"contract\":\"" << JsonEscape(contract_key) << "\","
        << "\"side\":\"" << (is_long ? "long" : "short") << "\","
        << "\"size\":" << size
        << "}";
    AppendLine(sc, outbox_dir, oss.str());
}

// Working limit order at `price`.
inline void EmitPlaceLimit(SCStudyInterfaceRef sc, const char* outbox_dir,
                           const std::string& sc_internal_id, int version,
                           const char* contract_key, bool is_long, int size,
                           float price)
{
    const std::string tag = MakeTag(sc_internal_id, version);
    char px_buf[32];
    std::snprintf(px_buf, sizeof(px_buf), "%.5f", price);
    std::ostringstream oss;
    oss << "{"
        << "\"ts\":\"" << NowIsoUtc() << "\","
        << "\"cmd\":\"place_limit\","
        << "\"tag\":\"" << JsonEscape(tag) << "\","
        << "\"sc_id\":\"" << JsonEscape(sc_internal_id) << "\","
        << "\"version\":" << version << ","
        << "\"contract\":\"" << JsonEscape(contract_key) << "\","
        << "\"side\":\"" << (is_long ? "long" : "short") << "\","
        << "\"size\":" << size << ","
        << "\"price\":" << px_buf
        << "}";
    AppendLine(sc, outbox_dir, oss.str());
}

// Working stop order at `stop_price`.
inline void EmitPlaceStop(SCStudyInterfaceRef sc, const char* outbox_dir,
                          const std::string& sc_internal_id, int version,
                          const char* contract_key, bool is_long, int size,
                          float stop_price)
{
    const std::string tag = MakeTag(sc_internal_id, version);
    char px_buf[32];
    std::snprintf(px_buf, sizeof(px_buf), "%.5f", stop_price);
    std::ostringstream oss;
    oss << "{"
        << "\"ts\":\"" << NowIsoUtc() << "\","
        << "\"cmd\":\"place_stop\","
        << "\"tag\":\"" << JsonEscape(tag) << "\","
        << "\"sc_id\":\"" << JsonEscape(sc_internal_id) << "\","
        << "\"version\":" << version << ","
        << "\"contract\":\"" << JsonEscape(contract_key) << "\","
        << "\"side\":\"" << (is_long ? "long" : "short") << "\","
        << "\"size\":" << size << ","
        << "\"stop_price\":" << px_buf
        << "}";
    AppendLine(sc, outbox_dir, oss.str());
}

// Cancel by sc_internal_id. The bridge looks up the current TopstepX order id
// for this sc_id in its state map and cancels it. Idempotent - if already
// gone, the bridge logs a warning and acks clean.
inline void EmitCancel(SCStudyInterfaceRef sc, const char* outbox_dir,
                       const std::string& sc_internal_id,
                       const char* contract_key)
{
    std::ostringstream oss;
    oss << "{"
        << "\"ts\":\"" << NowIsoUtc() << "\","
        << "\"cmd\":\"cancel\","
        << "\"tag\":\"manual-" << JsonEscape(sc_internal_id) << "-cancel\","
        << "\"sc_id\":\"" << JsonEscape(sc_internal_id) << "\","
        << "\"contract\":\"" << JsonEscape(contract_key) << "\""
        << "}";
    AppendLine(sc, outbox_dir, oss.str());
}

// Cancel-replace for a working order whose price/size changed. The bridge
// cancels the current TopstepX order for this sc_id, then places a new order
// at the new price/size. The new order gets tag "manual-{sc_id}-v{new_version}".
inline void EmitCancelReplace(SCStudyInterfaceRef sc, const char* outbox_dir,
                              const std::string& sc_internal_id, int new_version,
                              const char* contract_key, const char* new_type,
                              bool is_long, int size,
                              float price, float stop_price)
{
    const std::string tag = MakeTag(sc_internal_id, new_version);
    char limit_buf[32];
    char stop_buf[32];
    std::snprintf(limit_buf, sizeof(limit_buf), "%.5f", price);
    std::snprintf(stop_buf, sizeof(stop_buf), "%.5f", stop_price);
    std::ostringstream oss;
    oss << "{"
        << "\"ts\":\"" << NowIsoUtc() << "\","
        << "\"cmd\":\"cancel_replace\","
        << "\"tag\":\"" << JsonEscape(tag) << "\","
        << "\"sc_id\":\"" << JsonEscape(sc_internal_id) << "\","
        << "\"version\":" << new_version << ","
        << "\"contract\":\"" << JsonEscape(contract_key) << "\","
        << "\"new_type\":\"" << JsonEscape(new_type) << "\","
        << "\"side\":\"" << (is_long ? "long" : "short") << "\","
        << "\"size\":" << size;
    if (std::string(new_type) == "limit")
        oss << ",\"price\":" << limit_buf;
    else if (std::string(new_type) == "stop")
        oss << ",\"stop_price\":" << stop_buf;
    oss << "}";
    AppendLine(sc, outbox_dir, oss.str());
}

// A MIRRORED stop/limit went working->FILLED on Sierra Chart. The bridge
// checks whether the TopstepX twin for this sc_id is still resting; if it is,
// TopstepX did NOT fill (reprice-into-market on Sierra Chart, or a limit that
// filled here but not there) -> cancel the twin and fire a market for the
// same side/size so the shadow catches up. If the twin is already gone,
// no-op. The market is gated on the cancel SUCCEEDING, so it can never
// double-fill. Tag: "manual-{sc_id}-v{version}-close".
inline void EmitCloseIfOpen(SCStudyInterfaceRef sc, const char* outbox_dir,
                            const std::string& sc_internal_id, int version,
                            const char* contract_key, bool is_long, int size)
{
    std::ostringstream oss;
    oss << "{"
        << "\"ts\":\"" << NowIsoUtc() << "\","
        << "\"cmd\":\"close_if_open\","
        << "\"tag\":\"" << JsonEscape(MakeTag(sc_internal_id, version)) << "-close\","
        << "\"sc_id\":\"" << JsonEscape(sc_internal_id) << "\","
        << "\"version\":" << version << ","
        << "\"contract\":\"" << JsonEscape(contract_key) << "\","
        << "\"side\":\"" << (is_long ? "long" : "short") << "\","
        << "\"size\":" << size
        << "}";
    AppendLine(sc, outbox_dir, oss.str());
}

}  // namespace tsx_manual

#endif  // TSX_MANUAL_EMIT_H
