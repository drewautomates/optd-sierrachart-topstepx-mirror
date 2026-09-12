// Manual_Mirror.cpp - TopstepX manual-trade mirror study (Sierra Chart ACSIL)
// Copyright (c) 2026 Andrew Thomas (OPTD, onepersontradedesk.com)
// MIT License - see LICENSE, which also carries the risk disclosure.
// This code places real orders on a real account. You are responsible
// for every order it sends.
//
// Watches the order list of the trade account selected on this chart and
// mirrors every NEW order it sees (market / limit / stop) to a TopstepX
// account, by appending one JSON line per order state transition to an
// outbox file that the companion Python service (bridge/manual_bridge.py)
// tails and turns into TopstepX REST calls.
//
// One-way. Sierra Chart is the brain, TopstepX is the shadow. Nothing on
// TopstepX ever feeds back into Sierra Chart.
//
// ACCOUNT SCOPE
//   Sierra Chart's order list is inherently per symbol + trade account. The
//   study scans the trade account SELECTED ON THIS CHART only; orders on any
//   other account (for example copy-trade followers) are invisible to it, so
//   they never double-mirror. Flipping Sierra Chart's global Sim mode on/off
//   is fine - the order-list toggle below follows it. The study's Enable
//   input is the master switch.
//
// SINGLE INSTANCE
//   Run exactly ONE Manual_Mirror per Sierra Chart process. sc.GetOrderByIndex()
//   only returns orders matching the Symbol + Trade Account of the chart the
//   study sits on (it is NOT process-wide), so the scan iterates
//   sc.GetOrderForSymbolAndAccountByIndex() over every EXACT symbol in the
//   "Scan Symbols" input (edit at contract roll, e.g. ESU6.CME -> ESZ6.CME)
//   on this chart's selected trade account. Each order's TopstepX contract key
//   resolves via the Symbol->Contract map (longest match wins, so MES beats ES
//   and MNQ beats NQ). A second instance is refused at run time by the
//   owner_chart guard: the instances would share DLL-level static state and
//   garbage-collect each other's tracked orders, silently dropping cancels.
//
//   The study cannot tell a hand-placed order from one placed by an automated
//   trading study - s_SCTradeOrder carries no such flag. Any automated study
//   trading the scanned symbols on the selected account in the same Sierra
//   Chart process WILL be mirrored too. Keep automation in a separate Sierra
//   Chart instance, or put it in a different chartbook and turn on
//   "Scope To This Chartbook".
//
// BOOTSTRAP RULE
//   On the first scan after the study is enabled, every order already visible
//   on the account is recorded as "seen" and NOT mirrored. Only orders placed
//   after the study is running mirror. Start flat.
//
// FRESHNESS GUARD
//   A first-seen order is mirrored ONLY if its LastActivityTime is within the
//   "Freshness Window (ms)" input (default 1000 ms). Older first sightings are
//   history that appeared in the order list because of a trade-account switch,
//   a study reload, or a global Sim-mode toggle - they are recorded as seen
//   but NOT mirrored, so they can never be replayed onto TopstepX as live
//   orders. The gate is admission control at birth only: once an order is
//   mirrored it is tracked for its whole life (fills / modifies / cancels
//   mirror normally with no further freshness check), so long-resting limit
//   and stop orders are unaffected.
//
// SCAN CADENCE
//   The study runs on every chart update (sc.UpdateAlways = 1), throttled by
//   "Scan Throttle (ms)". The effective cadence is therefore the LARGER of the
//   throttle and Sierra Chart's Chart Update Interval (Global Settings ->
//   General Settings; default 500 ms). Lower the update interval if you want
//   the 50 ms default throttle to mean anything. The bootstrap banner prints
//   both numbers.
//
// LIFECYCLE DETECTION (in-memory snapshot per InternalOrderID)
//   - new working order (market/limit/stop) -> emit place_{type}
//   - new order first seen already FILLED    -> emit place_market (catch-up)
//   - working -> canceled                    -> emit cancel
//   - working -> filled                      -> no-op (TopstepX twin fills on
//                                               its own trigger); with
//                                               "Close-If-Open On Fill" = Yes
//                                               a filled stop/limit emits
//                                               close_if_open instead
//   - working price/qty change               -> emit cancel_replace (TopstepX
//                                               has no in-place modify)
//   - order vanishes while still working     -> emit cancel, only if Sierra
//                                               Chart confirms it CANCELED
//
// CLOSE-IF-OPEN - the working -> filled gap
//   A stop or limit that is REPRICED into the market and fills within a few
//   milliseconds (drag a stop through price to exit) is only ever seen as
//   working -> FILLED. No polling cadence can catch the working window, so
//   without help the TopstepX twin keeps its OLD price and the shadow
//   position rides unmanaged until that price trades. Same class: a Sierra
//   Chart limit that fills while the TopstepX twin does not (queue position).
//   With "Close-If-Open On Fill" = Yes the study emits close_if_open on every
//   mirrored stop/limit fill; the bridge cancels the twin and fires a market
//   for the same side/size ONLY if that cancel succeeds (twin still resting =
//   TopstepX did not fill), so it can never double-fill. Default OFF: run the
//   live test in README "Close-If-Open" first.
//
// OCO
//   No special handling. When Sierra Chart auto-cancels the sibling leg on a
//   bracket fill, the diff picks up the working -> canceled transition on the
//   sibling and emits a cancel. Small race window (about one scan interval
//   plus bridge latency) in which the TopstepX sibling could fill first.
//
// SUPPORTED ORDER TYPES: MARKET, LIMIT, STOP. STOP_LIMIT and every exotic
// type (trailing stop, MIT, ...) are logged and skipped.

#include "sierrachart.h"
#include "tsx_manual_emit.h"

#include <cctype>
#include <chrono>
#include <cstdio>
#include <map>
#include <set>
#include <string>
#include <vector>

SCDLLName("Manual_Mirror")

namespace {

struct OrderSnapshot {
    int    order_type;    // SCT_ORDERTYPE_* (MARKET/LIMIT/STOP)
    int    status;        // SCT_OSC_*
    int    buy_sell;      // BSE_BUY / BSE_SELL
    int    quantity;
    double price1;        // limit or stop trigger price
    double price2;        // stop-limit limit price (unused here)
    int    version;       // bumps on each cancel_replace
    bool   mirrored;      // did we emit a place_* for this sc order?
    std::string contract_key;  // resolved TopstepX key (for cancel/replace emits)
};

inline bool IsWorkingStatus(int code) {
    return code == SCT_OSC_ORDERSENT
        || code == SCT_OSC_PENDINGOPEN
        || code == SCT_OSC_OPEN
        || code == SCT_OSC_PENDINGMODIFY
        || code == SCT_OSC_PENDINGCANCEL
        || code == SCT_OSC_PENDING_CHILD_CLIENT
        || code == SCT_OSC_PENDING_CHILD_SERVER;
}

inline bool IsMirrorableType(int t) {
    return t == SCT_ORDERTYPE_MARKET
        || t == SCT_ORDERTYPE_LIMIT
        || t == SCT_ORDERTYPE_STOP;
}

inline const char* TypeName(int t) {
    switch (t) {
        case SCT_ORDERTYPE_MARKET: return "market";
        case SCT_ORDERTYPE_LIMIT:  return "limit";
        case SCT_ORDERTYPE_STOP:   return "stop";
        default:                   return "";
    }
}

inline uint64_t NowMs() {
    return (uint64_t)std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

inline std::string IntToString(int v) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%d", v);
    return std::string(buf);
}

// Trim ASCII whitespace from both ends (in place).
inline void TrimAscii(std::string& s) {
    size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) { s.clear(); return; }
    size_t e = s.find_last_not_of(" \t\r\n");
    s = s.substr(b, e - b + 1);
}

// Resolve a Sierra Chart order Symbol to a TopstepX contract key using a
// ';'-delimited map of "substr=key" tokens (a bare "X" token means "X=X").
// Returns the key whose substr is contained in `symbol`, preferring the
// LONGEST matching substr so "MES" wins over "ES" and "MNQ" over "NQ".
// Returns "" if nothing matches.
// Where the outbox lives when the "Outbox Directory" input is left blank.
//
// Inside a study DLL, GetModuleFileNameA(NULL, ...) returns the path of the
// process that loaded us - SierraChart.exe - so this resolves to
//     <Sierra Chart install folder>\sc-topstepx-mirror\outbox
// which is exactly where the README tells you to clone the repo. That makes
// the normal setup zero-configuration on any machine, drive or install path,
// and it is the same folder the bridge finds on its own when paths.outbox is
// left unset.
//
// If the repo was cloned somewhere else, this folder will not exist. The
// parent is missing, so AppendLine's CreateDirectoryA cannot create it and
// the open fails loudly at error level naming the path. That is deliberate:
// a wrong path that announces itself beats a blank input that quietly
// mirrors nothing.
inline std::string DeriveDefaultOutboxDir() {
    char exe_path[MAX_PATH] = {0};
    const DWORD written = GetModuleFileNameA(NULL, exe_path, MAX_PATH);
    if (written == 0 || written >= MAX_PATH) return std::string();
    const std::string full(exe_path, written);
    const size_t slash = full.find_last_of("\\/");
    if (slash == std::string::npos) return std::string();
    return full.substr(0, slash) + "\\sc-topstepx-mirror\\outbox";
}

inline std::string ResolveContractKey(const char* symbol, const char* map_input) {
    std::string best_key;
    size_t best_len = 0;
    const std::string s(map_input ? map_input : "");
    size_t start = 0;
    while (start <= s.size()) {
        size_t semi = s.find(';', start);
        std::string tok = (semi == std::string::npos)
                          ? s.substr(start)
                          : s.substr(start, semi - start);
        if (!tok.empty()) {
            size_t eq = tok.find('=');
            std::string sub = (eq == std::string::npos) ? tok : tok.substr(0, eq);
            std::string key = (eq == std::string::npos) ? tok : tok.substr(eq + 1);
            TrimAscii(sub);
            TrimAscii(key);
            if (!sub.empty() && !key.empty()
                && strstr(symbol, sub.c_str()) != nullptr
                && sub.size() > best_len) {
                best_len = sub.size();
                best_key = key;
            }
        }
        if (semi == std::string::npos) break;
        start = semi + 1;
    }
    return best_key;
}

// Split a ';'-delimited list into trimmed, non-empty tokens.
inline void SplitSemiList(const char* input, std::vector<std::string>& out) {
    const std::string s(input ? input : "");
    size_t start = 0;
    while (start <= s.size()) {
        size_t semi = s.find(';', start);
        std::string tok = (semi == std::string::npos)
                          ? s.substr(start)
                          : s.substr(start, semi - start);
        TrimAscii(tok);
        if (!tok.empty()) out.push_back(tok);
        if (semi == std::string::npos) break;
        start = semi + 1;
    }
}

// Normalize a chartbook identifier for comparison: strip any directory prefix
// and a trailing ".Cht" extension, lowercase, trim. Lets an order's
// SourceChartbookFileName (which may be a full path or bare name, with or
// without extension) be matched against sc.ChartbookName() robustly.
inline std::string ChartbookKey(const char* raw) {
    std::string x(raw ? raw : "");
    size_t slash = x.find_last_of("\\/");
    if (slash != std::string::npos) x = x.substr(slash + 1);
    if (x.size() >= 4) {
        std::string ext = x.substr(x.size() - 4);
        for (char& c : ext) c = (char)std::tolower((unsigned char)c);
        if (ext == ".cht") x = x.substr(0, x.size() - 4);
    }
    for (char& c : x) c = (char)std::tolower((unsigned char)c);
    TrimAscii(x);
    return x;
}

// Effective mirror size for an order = max(OrderQuantity, FilledQuantity).
// Sierra Chart's behavior across fill states is inconsistent (OrderQuantity
// sometimes stays at original, sometimes decrements on fill) and partial
// fills grow FilledQuantity; max() gives the intended size in every state:
// fresh (5,0)->5, partial (5,2)->5, full-fill-stable (5,5)->5,
// full-fill-reset (0,5)->5. Bracket children (attached stop/target) inherit
// qty from the parent - Sierra Chart leaves the child's OrderQuantity at 0
// until the parent fills - so fall back to the parent. Used by BOTH the
// first-seen and the known-order paths: using it only on first-seen made a
// child read 0 on the next scan, flagging a phantom size change that later
// fired a same-price cancel_replace on TopstepX. Returns 0 if unresolvable.
inline int ResolveEffectiveQty(SCStudyInterfaceRef sc, const s_SCTradeOrder& od) {
    int q = od.OrderQuantity;
    if (od.FilledQuantity > q) q = od.FilledQuantity;
    if (q <= 0 && od.ParentInternalOrderID > 0) {
        s_SCTradeOrder parent_od;
        if (sc.GetOrderByOrderID(od.ParentInternalOrderID, parent_od)
            != SCTRADING_ORDER_ERROR) {
            q = parent_od.OrderQuantity;
            if (parent_od.FilledQuantity > q) q = parent_od.FilledQuantity;
        }
    }
    return q;
}

}  // namespace

SCSFExport scsf_ManualMirror(SCStudyInterfaceRef sc) {

    SCInputRef In_Enable         = sc.Input[0];
    SCInputRef In_SymbolMap      = sc.Input[1];
    SCInputRef In_ThrottleMs     = sc.Input[2];
    SCInputRef In_Logging        = sc.Input[3];
    SCInputRef In_FreshnessMs    = sc.Input[4];
    SCInputRef In_ScopeChartbook = sc.Input[5];
    SCInputRef In_ScanSymbols    = sc.Input[6];
    SCInputRef In_OutboxDir      = sc.Input[7];
    SCInputRef In_CloseIfOpen    = sc.Input[8];

    if (sc.SetDefaults) {
        sc.GraphName = "TopstepX Manual Mirror";
        sc.StudyDescription =
            "Mirrors manual Sierra Chart orders (market/limit/stop) on every EXACT "
            "symbol listed in Scan Symbols (edit at contract roll), for this chart's "
            "selected trade account, to TopstepX via a JSONL outbox that "
            "manual_bridge.py tails. Sierra Chart's order list is filtered per "
            "symbol+account, so symbols NOT in the list are invisible and never "
            "mirror. Run exactly ONE instance per Sierra Chart process. Bootstrap "
            "snapshot on enable - only NEW orders placed after the study is running "
            "mirror. Scope To This Chartbook (default off) restricts mirroring to "
            "orders placed from this study's own chartbook, so automated studies in "
            "another chartbook of the same instance are excluded.";

        sc.AutoLoop = 0;
        sc.UpdateAlways = 1;
        sc.GraphRegion = 0;

        sc.SupportAttachedOrdersForTrading = 0;
        sc.SupportReversals = 0;
        sc.AllowMultipleEntriesInSameDirection = 0;
        sc.CancelAllOrdersOnEntriesAndReversals = 0;
        sc.AllowEntryWithWorkingOrders = 0;
        sc.CancelAllWorkingOrdersOnExit = 0;
        sc.MaintainTradeStatisticsAndTradesData = 0;

        In_Enable.Name = "Enable Manual Mirror";
        In_Enable.SetYesNo(0);

        In_SymbolMap.Name = "Symbol->Contract Map (substr=key; ...)";
        In_SymbolMap.SetString("MNQ=MNQ;NQ=NQ;ES=ES;MES=MES");

        In_ThrottleMs.Name = "Scan Throttle (ms)";
        In_ThrottleMs.SetInt(50);
        In_ThrottleMs.SetIntLimits(50, 5000);

        In_Logging.Name = "Verbose Logging";
        In_Logging.SetYesNo(0);

        In_FreshnessMs.Name = "Freshness Window (ms)";
        In_FreshnessMs.SetInt(1000);
        In_FreshnessMs.SetIntLimits(200, 5000);

        In_ScopeChartbook.Name = "Scope To This Chartbook";
        In_ScopeChartbook.SetYesNo(0);

        // Deliberately blank. The exact symbol string differs by data feed
        // (ESU6.CME vs ESU26_FUT_CME vs ...), and a wrong default would fail
        // SILENTLY - a symbol not in this list simply never mirrors. Blank
        // fails loud instead (see the warning in the runtime body).
        In_ScanSymbols.Name = "Scan Symbols (exact, ';' sep) EDIT AT ROLL";
        In_ScanSymbols.SetString("");

        // Blank means "work it out": DeriveDefaultOutboxDir() resolves
        // <Sierra Chart folder>\sc-topstepx-mirror\outbox, which is where the
        // README says to clone the repo, and is also what the bridge picks by
        // itself when paths.outbox is left unset. So the normal case is: leave
        // this alone. Set it only to point somewhere else - and then it must
        // equal the bridge's outbox, which
        // 'python manual_bridge.py --doctor' prints for you.
        In_OutboxDir.Name = "Outbox Directory (blank = beside Sierra Chart)";
        In_OutboxDir.SetString("");

        // When a MIRRORED stop/limit goes working->FILLED, emit close_if_open
        // so the bridge cancels the TopstepX twin and, ONLY if that cancel
        // succeeds (twin was still resting = TopstepX did not fill), fires a
        // market for the same side/size. Default OFF: flip only after the
        // live test in README "Close-If-Open" (an input flip, no rebuild).
        In_CloseIfOpen.Name = "Close-If-Open On Fill (stop/limit)";
        In_CloseIfOpen.SetYesNo(0);

        return;
    }

    // DLL-level statics: shared across every instance of this study in the
    // Sierra Chart process. This study is a SINGLE instance by design - it
    // scans every symbol in the Scan Symbols input via
    // GetOrderForSymbolAndAccountByIndex. The owner_chart guard below refuses
    // to run a second instance, which would corrupt this shared state (each
    // instance would garbage-collect the other's tracked orders). Run ONE.
    static std::map<int, OrderSnapshot> last_seen;
    static std::set<int> probe_logged;  // orders we've printed a diagnostic line for
    static bool bootstrapped = false;
    static uint64_t last_scan_ms = 0;
    static int owner_chart = -1;        // ChartNumber of the single active instance

    if (sc.LastCallToFunction) {
        // Only the owning instance clears the shared state and releases the
        // claim (so removing a non-owner instance can't wipe the owner's
        // snapshot).
        if (owner_chart == sc.ChartNumber) {
            last_seen.clear();
            probe_logged.clear();
            bootstrapped = false;
            last_scan_ms = 0;
            owner_chart = -1;
        }
        return;
    }

    if (!In_Enable.GetYesNo()) return;

    // Single-instance guard. One instance scans all configured symbols, so one
    // is sufficient AND required: a second instance shares the statics above
    // and the two would erase each other's tracked orders, silently dropping
    // cancels. The first enabled instance claims ownership; others log once
    // and idle.
    if (owner_chart == -1) owner_chart = sc.ChartNumber;
    if (owner_chart != sc.ChartNumber) {
        static std::set<int> warned_charts;
        if (warned_charts.find(sc.ChartNumber) == warned_charts.end()) {
            SCString msg;
            msg.Format("Manual_Mirror: IDLE on chart %d - another instance (chart %d) "
                       "is already active. Run ONE instance per Sierra Chart process; "
                       "it mirrors every symbol in its Scan Symbols list. Remove the "
                       "extra instance(s).", sc.ChartNumber, owner_chart);
            sc.AddMessageToLog(msg, 1);
            warned_charts.insert(sc.ChartNumber);
        }
        return;
    }

    // Scope the order-list iterators to match the current global Sim state.
    // Sierra Chart returns ONLY sim orders when this is false (the default)
    // and ONLY live/non-simulated orders when this is true. Without this
    // line, live orders would be invisible to the study while Sim orders
    // would mirror fine.
    sc.SendOrdersToTradeService = !sc.GlobalTradeSimulationIsOn;

    const uint64_t throttle_ms = (uint64_t)In_ThrottleMs.GetInt();
    const uint64_t now_ms = NowMs();
    if (now_ms - last_scan_ms < throttle_ms) return;
    last_scan_ms = now_ms;

    const char* symbol_map = In_SymbolMap.GetString();
    const bool verbose = In_Logging.GetYesNo() != 0;
    const bool close_if_open = In_CloseIfOpen.GetYesNo() != 0;

    // Optional chartbook scoping. When on, only mirror orders whose source
    // chartbook matches THIS study's chartbook (auto-detected, nothing
    // hardcoded). Fail-closed: an order with a blank/mismatched
    // SourceChartbookFileName is skipped (a miss beats a double-fire). Enable
    // Verbose Logging to see book= per order and PROVE your order-entry path
    // populates the field before relying on this.
    const bool scope_book = In_ScopeChartbook.GetYesNo() != 0;
    const std::string own_book_key = ChartbookKey(sc.ChartbookName().GetChars());

    // Freshness guard. A first-seen order is mirrored only if its last
    // activity (placement or fill) happened within this window. Anything
    // older appeared from history - another account's orders after a
    // trade-account switch, a study reload, or a global Sim-mode toggle - and
    // must NOT be replayed onto TopstepX as live orders. Floor the window at
    // throttle + 750 ms so it can never be tighter than the scan cadence
    // (only matters if Throttle is raised well above the 50 ms default).
    uint64_t freshness_ms = (uint64_t)In_FreshnessMs.GetInt();
    if (freshness_ms < throttle_ms + 750) freshness_ms = throttle_ms + 750;
    const double freshness_sec = (double)freshness_ms / 1000.0;
    const double now_days = sc.GetCurrentDateTime().GetAsDouble();

    // Sierra Chart's internal order list is filtered per Symbol + Trade
    // Account: sc.GetOrderByIndex() would only ever return orders matching
    // THIS chart's symbol, so instead iterate
    // sc.GetOrderForSymbolAndAccountByIndex() over every exact symbol in the
    // Scan Symbols input, on this chart's selected trade account. A symbol
    // missing from the list is invisible - it can never mirror. Collect the
    // orders first, then diff them below.
    const char* account = sc.SelectedTradeAccount.GetChars();
    static bool warned_no_account = false;
    if (account == nullptr || account[0] == '\0') {
        if (!warned_no_account) {
            sc.AddMessageToLog(
                "Manual_Mirror: no trade account selected on this chart - "
                "cannot scan orders. Select the account to mirror.", 1);
            warned_no_account = true;
        }
        return;
    }
    warned_no_account = false;

    std::vector<std::string> scan_symbols;
    SplitSemiList(In_ScanSymbols.GetString(), scan_symbols);
    static bool warned_no_symbols = false;
    if (scan_symbols.empty()) {
        if (!warned_no_symbols) {
            sc.AddMessageToLog(
                "Manual_Mirror: Scan Symbols input is empty - nothing to mirror. "
                "Set the EXACT symbols as shown in the chart header, ';' separated "
                "(e.g. ESZ6.CME;MESZ6.CME). A symbol not listed never mirrors.", 1);
            warned_no_symbols = true;
        }
        return;
    }
    warned_no_symbols = false;

    std::string outbox_dir(In_OutboxDir.GetString());
    TrimAscii(outbox_dir);
    static bool warned_no_outbox = false;
    static bool logged_derived_outbox = false;
    if (outbox_dir.empty()) {
        outbox_dir = DeriveDefaultOutboxDir();
        if (!outbox_dir.empty() && !logged_derived_outbox) {
            // Say which folder was chosen, once. A derived path that nobody
            // can see is just a different way to be silently wrong.
            SCString derived_msg;
            derived_msg.Format(
                "Manual_Mirror: Outbox Directory input is blank - using the "
                "default beside Sierra Chart: %s | This matches the bridge only "
                "if the repo was cloned there. Check it against the path printed "
                "by 'python manual_bridge.py --doctor'.", outbox_dir.c_str());
            sc.AddMessageToLog(derived_msg, 0);
            logged_derived_outbox = true;
        }
    }
    if (outbox_dir.empty()) {
        if (!warned_no_outbox) {
            sc.AddMessageToLog(
                "Manual_Mirror: Outbox Directory input is empty and no default "
                "could be derived - nothing can be emitted. Set it to the folder "
                "the bridge reads, which 'python manual_bridge.py --doctor' "
                "prints.", 1);
            warned_no_outbox = true;
        }
        return;
    }
    warned_no_outbox = false;
    const char* outbox = outbox_dir.c_str();

    std::vector<s_SCTradeOrder> scanned_orders;
    for (const std::string& scan_sym : scan_symbols) {
        int sym_idx = 0;
        s_SCTradeOrder sym_od;
        while (sc.GetOrderForSymbolAndAccountByIndex(scan_sym.c_str(), account,
                                                     sym_idx++, sym_od)
               != SCTRADING_ORDER_ERROR) {
            scanned_orders.push_back(sym_od);
        }
    }

    // Resolve each order's Symbol to a TopstepX contract key via the
    // Symbol->Contract map (longest match wins so "MES" beats "ES", "MNQ"
    // beats "NQ"). An order whose Symbol matches no map entry is ignored.
    std::set<int> current_ids;
    for (s_SCTradeOrder& od : scanned_orders) {
        const std::string okey = ResolveContractKey(od.Symbol.GetChars(), symbol_map);
        const bool sym_match = !okey.empty();
        if (verbose && probe_logged.find(od.InternalOrderID) == probe_logged.end()) {
            SCString msg;
            msg.Format("Manual_Mirror: SEE sc_id=%d od.Symbol='%s' key='%s' (match=%d) "
                       "od.Account='%s' book='%s' srcChart=%d status=%d type=%d qty=%d",
                       od.InternalOrderID,
                       od.Symbol.GetChars(), okey.c_str(), sym_match ? 1 : 0,
                       od.TradeAccount.GetChars(),
                       od.SourceChartbookFileName.GetChars(), od.SourceChartNumber,
                       od.OrderStatusCode, od.OrderTypeAsInt, od.OrderQuantity);
            sc.AddMessageToLog(msg, 0);
            probe_logged.insert(od.InternalOrderID);
        }
        if (!sym_match) continue;

        // Chartbook scope filter (opt-in). Skip orders not born in this
        // study's own chartbook so automated studies in another chartbook are
        // never mirrored.
        if (scope_book
            && ChartbookKey(od.SourceChartbookFileName.GetChars()) != own_book_key) {
            continue;
        }
        // Per-order resolved key. Used for all first-seen emits below; the
        // snapshot stores it so cancel/replace paths emit the right contract
        // even if the symbol later flickers.
        const char* contract_key = okey.c_str();

        const int oid = od.InternalOrderID;
        current_ids.insert(oid);

        auto it = last_seen.find(oid);
        if (it == last_seen.end()) {
            // First time seeing this order id.
            OrderSnapshot snap;
            snap.order_type = od.OrderTypeAsInt;
            snap.status     = od.OrderStatusCode;
            snap.buy_sell   = od.BuySell;
            snap.quantity   = (od.OrderQuantity > od.FilledQuantity)
                              ? od.OrderQuantity : od.FilledQuantity;
            snap.price1     = od.Price1;
            snap.price2     = od.Price2;
            snap.version    = 1;
            snap.mirrored   = false;
            snap.contract_key = okey;

            if (!bootstrapped) {
                // Bootstrap pass: record, don't emit.
                last_seen[oid] = snap;
                continue;
            }

            // Freshness guard (see file header). If this order's last
            // activity is older than the window, it is history that just
            // appeared in the list, not a live action - record it as seen
            // (mirrored=false, so the cancel/vanish paths below never emit
            // for it) and skip. This is the primary protection against
            // replaying a different account's order/fill history as live
            // TopstepX orders. age_sec < -1.0 also rejects "future"
            // timestamps (clock/TZ skew), failing safe toward NOT firing.
            {
                SCDateTime act_ts = od.LastActivityTime;
                if (act_ts.GetAsDouble() <= 0.0) act_ts = od.EntryDateTime;
                const double age_sec = (now_days - act_ts.GetAsDouble()) * 86400.0;
                if (age_sec < -1.0 || age_sec > freshness_sec) {
                    last_seen[oid] = snap;  // seen, mirrored=false
                    if (verbose) {
                        SCString msg;
                        msg.Format("Manual_Mirror: STALE skip sc_id=%d age=%.2fs "
                                   "(window=%.2fs status=%d type=%d) - not mirrored",
                                   oid, age_sec, freshness_sec,
                                   od.OrderStatusCode, od.OrderTypeAsInt);
                        sc.AddMessageToLog(msg, 0);
                    }
                    continue;
                }
            }

            if (!IsMirrorableType(od.OrderTypeAsInt)) {
                SCString msg;
                msg.Format("Manual_Mirror: SKIP unsupported order type (int=%d) sc_id=%d",
                           od.OrderTypeAsInt, oid);
                sc.AddMessageToLog(msg, 0);
                last_seen[oid] = snap;
                continue;
            }

            const bool is_working = IsWorkingStatus(od.OrderStatusCode);
            const bool is_filled  = (od.OrderStatusCode == SCT_OSC_FILLED);

            if (!is_working && !is_filled) {
                // CANCELED / ERROR on first sight - nothing to mirror.
                last_seen[oid] = snap;
                continue;
            }

            const std::string sc_id = IntToString(oid);
            const bool is_long = (od.BuySell == BSE_BUY);

            // max(OrderQty, FilledQty) with the parent fallback for bracket
            // children - see ResolveEffectiveQty.
            const int effective_qty = ResolveEffectiveQty(sc, od);

            if (effective_qty <= 0) {
                if (verbose) {
                    SCString msg;
                    msg.Format("Manual_Mirror: DEFER zero-qty sc_id=%d "
                               "parent=%d OrderQty=%d FilledQty=%d status=%d",
                               oid, od.ParentInternalOrderID,
                               od.OrderQuantity, od.FilledQuantity,
                               od.OrderStatusCode);
                    sc.AddMessageToLog(msg, 0);
                }
                continue;
            }

            if (is_filled) {
                // Fast market (or marketable limit/stop) raced through
                // Open -> Filled between scans. We never saw it working, so
                // there's no resting order to mirror - fire a market on
                // TopstepX instead to catch up on the position change.
                tsx_manual::EmitPlaceMarket(sc, outbox, sc_id, snap.version,
                                            contract_key, is_long,
                                            effective_qty);
                snap.mirrored = true;
                snap.quantity = effective_qty;
                last_seen[oid] = snap;
                if (verbose) {
                    SCString msg;
                    msg.Format("Manual_Mirror: CATCH-UP market sc_id=%d "
                               "(first-seen FILLED, orig type=%s) %s qty=%d",
                               oid, TypeName(od.OrderTypeAsInt),
                               is_long ? "BUY" : "SELL", effective_qty);
                    sc.AddMessageToLog(msg, 0);
                }
                continue;
            }

            // Normal case: first-seen while still working.
            switch (od.OrderTypeAsInt) {
                case SCT_ORDERTYPE_MARKET:
                    tsx_manual::EmitPlaceMarket(sc, outbox, sc_id, snap.version,
                                                contract_key, is_long,
                                                effective_qty);
                    break;
                case SCT_ORDERTYPE_LIMIT:
                    tsx_manual::EmitPlaceLimit(sc, outbox, sc_id, snap.version,
                                               contract_key, is_long,
                                               effective_qty, (float)od.Price1);
                    break;
                case SCT_ORDERTYPE_STOP:
                    tsx_manual::EmitPlaceStop(sc, outbox, sc_id, snap.version,
                                              contract_key, is_long,
                                              effective_qty, (float)od.Price1);
                    break;
            }
            snap.mirrored = true;
            snap.quantity = effective_qty;
            last_seen[oid] = snap;

            if (verbose) {
                SCString msg;
                msg.Format("Manual_Mirror: MIRROR sc_id=%d type=%s %s qty=%d px=%.2f",
                           oid, TypeName(od.OrderTypeAsInt),
                           is_long ? "BUY" : "SELL",
                           effective_qty, od.Price1);
                sc.AddMessageToLog(msg, 0);
            }
        } else {
            // Known order - look for transitions.
            OrderSnapshot& prev = it->second;
            const bool was_working = IsWorkingStatus(prev.status);
            const bool now_working = IsWorkingStatus(od.OrderStatusCode);
            // Same resolution as first-seen (parent fallback), so a bracket
            // child never reads 0 here and gets flagged as a size change.
            // 0 = unresolvable this scan = "no information".
            const int cur_qty = ResolveEffectiveQty(sc, od);

            if (was_working && !now_working) {
                if (od.OrderStatusCode == SCT_OSC_CANCELED && prev.mirrored) {
                    tsx_manual::EmitCancel(sc, outbox, IntToString(oid), contract_key);
                    if (verbose) {
                        SCString msg;
                        msg.Format("Manual_Mirror: CANCEL sc_id=%d", oid);
                        sc.AddMessageToLog(msg, 0);
                    }
                } else if (od.OrderStatusCode == SCT_OSC_FILLED && prev.mirrored
                           && close_if_open
                           && (prev.order_type == SCT_ORDERTYPE_LIMIT
                               || prev.order_type == SCT_ORDERTYPE_STOP)) {
                    // Close-if-open. A resting stop/limit that filled on
                    // Sierra Chart may NOT have filled on TopstepX: a
                    // reprice-into-market (drag the stop through price) fills
                    // in ms and only ever shows here as working->FILLED,
                    // leaving the twin at the OLD price; a limit can fill
                    // here and not there (queue). The bridge cancels the twin
                    // and fires a market for the same side/size ONLY if that
                    // cancel succeeds (twin still resting = TopstepX did not
                    // fill). Market orders are excluded: their twin never
                    // rests.
                    const bool is_long = (od.BuySell == BSE_BUY);
                    const int size = (prev.quantity > 0) ? prev.quantity : cur_qty;
                    if (size > 0) {
                        tsx_manual::EmitCloseIfOpen(sc, outbox, IntToString(oid),
                                                    prev.version, contract_key,
                                                    is_long, size);
                        if (verbose) {
                            SCString msg;
                            msg.Format("Manual_Mirror: CLOSE-IF-OPEN sc_id=%d v%d %s qty=%d "
                                       "(filled type=%s px=%.2f)",
                                       oid, prev.version, is_long ? "BUY" : "SELL", size,
                                       TypeName(prev.order_type), od.Price1);
                            sc.AddMessageToLog(msg, 0);
                        }
                    }
                }
                // SCT_OSC_FILLED with Close-If-Open=No: nothing - the twin fills on its own.
                // SCT_OSC_ERROR:  nothing - TopstepX order (if any) is independent.
            } else if (was_working && now_working && prev.mirrored) {
                // max(OrderQty, FilledQty) is stable across partial fills
                // so we don't false-positive them as size modifies. A 0 read
                // (unresolvable) is never a size change.
                const bool price_changed = (od.Price1 != prev.price1)
                                        || (od.Price2 != prev.price2);
                const bool size_changed  = (cur_qty > 0 && cur_qty != prev.quantity);
                const int emit_qty = (cur_qty > 0) ? cur_qty : prev.quantity;
                const char* type_name = TypeName(od.OrderTypeAsInt);
                if ((price_changed || size_changed)
                    && type_name[0] != '\0' && emit_qty > 0) {
                    // Bump the version ONLY when an emit actually goes out,
                    // so the bridge's version ladder has no holes.
                    prev.version += 1;
                    const bool is_long = (od.BuySell == BSE_BUY);
                    tsx_manual::EmitCancelReplace(
                        sc, outbox, IntToString(oid), prev.version,
                        contract_key, type_name, is_long,
                        emit_qty,
                        (float)od.Price1, (float)od.Price1);
                    if (verbose) {
                        SCString msg;
                        msg.Format("Manual_Mirror: REPLACE sc_id=%d v%d qty=%d px=%.2f",
                                   oid, prev.version, emit_qty, od.Price1);
                        sc.AddMessageToLog(msg, 0);
                    }
                }
            }

            prev.status = od.OrderStatusCode;
            if (cur_qty > 0) prev.quantity = cur_qty;  // never overwrite with 0
            prev.price1 = od.Price1;
            prev.price2 = od.Price2;
        }
    }

    // Garbage collect orders that disappeared from Sierra Chart's list. Fast
    // market orders can race through Open -> Filled -> GC'd between two
    // scans, so our last snapshot may still say "working" when the order
    // already filled. Do NOT emit a blind cancel - query the terminal status
    // via sc.GetOrderByOrderID and only cancel if it explicitly shows
    // CANCELED. If the order is truly gone and Sierra Chart can't tell us its
    // fate, no-op.
    for (auto it = last_seen.begin(); it != last_seen.end();) {
        if (current_ids.find(it->first) == current_ids.end()) {
            if (IsWorkingStatus(it->second.status) && it->second.mirrored) {
                s_SCTradeOrder term_od;
                int res = sc.GetOrderByOrderID(it->first, term_od);
                int term_status = (res != SCTRADING_ORDER_ERROR)
                                  ? term_od.OrderStatusCode
                                  : SCT_OSC_UNSPECIFIED;
                if (term_status == SCT_OSC_CANCELED) {
                    tsx_manual::EmitCancel(sc, outbox, IntToString(it->first),
                                           it->second.contract_key.c_str());
                    if (verbose) {
                        SCString msg;
                        msg.Format("Manual_Mirror: VANISH confirmed CANCEL sc_id=%d",
                                   it->first);
                        sc.AddMessageToLog(msg, 0);
                    }
                } else if (term_status == SCT_OSC_FILLED && close_if_open
                           && (it->second.order_type == SCT_ORDERTYPE_LIMIT
                               || it->second.order_type == SCT_ORDERTYPE_STOP)
                           && it->second.quantity > 0) {
                    // Vanished AND Sierra Chart confirms FILLED - same case
                    // as the in-list working->FILLED transition above.
                    tsx_manual::EmitCloseIfOpen(sc, outbox, IntToString(it->first),
                                                it->second.version,
                                                it->second.contract_key.c_str(),
                                                it->second.buy_sell == BSE_BUY,
                                                it->second.quantity);
                    if (verbose) {
                        SCString msg;
                        msg.Format("Manual_Mirror: VANISH confirmed FILLED -> CLOSE-IF-OPEN "
                                   "sc_id=%d qty=%d", it->first, it->second.quantity);
                        sc.AddMessageToLog(msg, 0);
                    }
                } else if (verbose) {
                    SCString msg;
                    msg.Format("Manual_Mirror: VANISH no-op sc_id=%d term_status=%d "
                               "(FILLED=%d CANCELED=%d UNSPECIFIED=%d)",
                               it->first, term_status,
                               SCT_OSC_FILLED, SCT_OSC_CANCELED, SCT_OSC_UNSPECIFIED);
                    sc.AddMessageToLog(msg, 0);
                }
            }
            it = last_seen.erase(it);
        } else {
            ++it;
        }
    }

    if (!bootstrapped) {
        bootstrapped = true;
        // Printed UNCONDITIONALLY (not behind Verbose Logging) so a log-based
        // health check can always see that the study is alive and how it is
        // configured. The two cadence numbers matter: the scan runs no
        // faster than the larger of them.
        SCString msg;
        msg.Format("Manual_Mirror: bootstrap done (%d existing orders marked seen, "
                   "not mirrored) account=%s scan_symbols=%s symbol_map=%s outbox=%s "
                   "close_if_open=%d throttle_ms=%d chart_update_ms=%d",
                   (int)last_seen.size(), account,
                   In_ScanSymbols.GetString(), symbol_map, outbox,
                   close_if_open ? 1 : 0, (int)throttle_ms,
                   sc.ChartUpdateIntervalInMilliseconds);
        sc.AddMessageToLog(msg, 0);
    }
}
