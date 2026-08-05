from __future__ import annotations

import pandas as pd
import streamlit as st
import sys
from pathlib import Path
from streamlit_autorefresh import st_autorefresh


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import initialize_database
from app.db.repository import PurchasingRepository
from app.services.case_service import (
    create_case,
    create_case_from_detected_items,
    list_cases,
)
from app.services.attachment_service import (
    list_case_attachments,
    save_case_attachment,
)
from app.services.rfq_detection_service import (
    RfqDetectionResult,
    detect_rfq_selection,
)
from app.services.supplier_catalog_service import (
    list_material_choices,
    list_suppliers_for_material,
)
from app.services.simple_chat_service import (
    build_item_supplier_overview,
    build_supplier_overview,
    build_supplier_rollup_overview,
    generate_and_send_winner_notification_for_case_item,
    generate_and_send_winner_notification_for_supplier,
    get_suggested_winner,
    record_supplier_message_simple,
    continue_negotiation_for_case,
    refresh_mailbox_and_continue_case,
    send_or_display_outbound_message,
    start_negotiating_case,
)
from app.services.human_review_service import (
    build_human_review_suggestions,
    resolve_human_review_with_reply,
    resolve_human_review_without_reply,
)

st.set_page_config(
    page_title="AI Purchase Assistant",
    layout="wide",
)


initialize_database()
repo = PurchasingRepository()

_OUTBOX_STATUS_LABELS = {
    "pending": "Queued",
    "processing": "Sending...",
    "sent": "Sent",
    "simulated": "Sent (simulated)",
    "transient_failure": "Retrying (temporary delivery failure)",
    "permanent_failure": "Failed - see human review",
    "delivery_unknown": "Delivery unknown - see human review",
}


def _describe_outbound_message_status(msg: dict) -> str:
    """Return a buyer-facing status label for one outbound message.

    Outbound email/WhatsApp messages are tracked in the transport outbox;
    show that status when available, since it reflects the real delivery
    state (including a pending automatic retry), not just the outcome of
    the very first attempt recorded on the messages row. Falls back to the
    raw message status for manual/simulated messages, which never get an
    outbox job.
    """
    if msg.get("direction") != "outbound" or msg.get("channel") not in (
        "email",
        "whatsapp",
    ):
        return msg.get("status") or ""

    outbox_job = repo.get_outbox_status_for_message(int(msg["id"]))

    if outbox_job is None:
        return msg.get("status") or ""

    return _OUTBOX_STATUS_LABELS.get(outbox_job["status"], outbox_job["status"])


# ---------------------------------------------------------------------
# Case creation dialog
# ---------------------------------------------------------------------

def _merge_rfq_detection(uploaded_files) -> RfqDetectionResult:
    """Run RFQ auto-detection across every uploaded file and merge hits.

    The buyer may attach several files at once (e.g. an RFQ plus a
    reference PDF); each is checked independently and any recognized items
    are combined into one list.
    """
    if not uploaded_files:
        return RfqDetectionResult(recognized=False)

    file_type = None
    items = []
    unresolved_lines = []
    recognized = False

    for uploaded_file in uploaded_files:
        result = detect_rfq_selection(uploaded_file.getvalue(), uploaded_file.name)
        if result.recognized:
            recognized = True
            file_type = file_type or result.file_type
            items.extend(result.items)
            unresolved_lines.extend(result.unresolved_lines)

    return RfqDetectionResult(
        recognized=recognized,
        file_type=file_type,
        items=items,
        unresolved_lines=unresolved_lines,
    )


def _attach_uploaded_files_to_cases(uploaded_files, case_ids: list[int]) -> list[str]:
    """Save every uploaded file as an attachment on every given case.

    One uploaded RFQ file can spawn several cases (one per detected item);
    each case keeps its own copy of the source file, since Phase 1's
    attachment model links one stored file to exactly one case.
    """
    errors = []
    for case_id in case_ids:
        for uploaded_file in uploaded_files or []:
            try:
                save_case_attachment(
                    case_id=case_id,
                    original_filename=uploaded_file.name,
                    file_bytes=uploaded_file.getvalue(),
                )
            except Exception as exc:
                errors.append(f"case {case_id} / {uploaded_file.name}: {exc}")

    return errors


def _render_detected_rfq_case_creation(
    detection: RfqDetectionResult,
    uploaded_files,
) -> None:
    """Auto-detected path: one case per recognized RFQ item, suppliers
    pre-selected from the catalog. The buyer can still remove suppliers or
    adjust quantity per item before creating the cases."""
    st.success(
        f"Recognized a {detection.file_type} RFQ with "
        f"{len(detection.items)} item(s). One case will be created per item."
    )

    if detection.unresolved_lines:
        with st.expander(
            f"{len(detection.unresolved_lines)} line(s) could not be matched "
            "to a catalog item",
            expanded=False,
        ):
            for line in detection.unresolved_lines:
                st.caption(line)

    with st.form("create_cases_from_rfq_form"):
        notes = st.text_area("Notes (applied to every case)", height=80)

        auto_send_messages = st.checkbox(
            "Send real messages for these cases",
            value=False,
            help=(
                "Checked: automatic buyer messages use each supplier's real "
                "email or WhatsApp channel. Unchecked: all outbound messages "
                "stay in the Streamlit chat for simulation."
            ),
        )

        notify_buyer_on_human_review = st.checkbox(
            "Email the buyer when human review is required",
            value=False,
            help=(
                "When checked, each newly created human-review item sends "
                "one internal notification email. The recipient is "
                "BUYER_REVIEW_NOTIFICATION_EMAIL, or BUYER_EMAIL as fallback."
            ),
        )

        pending_items = []

        for index, item in enumerate(detection.items):
            st.markdown(f"### {item.goods_name}")
            st.caption(f"From file: {item.description}")

            item_suppliers = list_suppliers_for_material(item.goods_name)
            if not item_suppliers:
                st.warning(
                    f"No active suppliers are linked to {item.goods_name}; "
                    "this item will be skipped."
                )
                continue

            item_supplier_labels = {
                (
                    f"{supplier['name']} | "
                    f"{supplier.get('contact_channel') or 'manual'} | "
                    f"{supplier.get('email') or supplier.get('whatsapp_number') or 'no contact'}"
                ): supplier["id"]
                for supplier in item_suppliers
            }

            item_quantity = st.number_input(
                f"Quantity — {item.goods_name}",
                min_value=0.01,
                value=float(item.quantity) if item.quantity else 1.0,
                step=1.0,
                key=f"rfq_item_quantity_{index}",
            )

            selected_item_supplier_labels = st.multiselect(
                f"Suppliers — {item.goods_name}",
                options=list(item_supplier_labels.keys()),
                default=list(item_supplier_labels.keys()),
                key=f"rfq_item_suppliers_{index}",
            )

            pending_items.append(
                {
                    "item_material": item.goods_name,
                    "quantity": item_quantity,
                    "supplier_ids": [
                        item_supplier_labels[label]
                        for label in selected_item_supplier_labels
                    ],
                }
            )

        pending_supplier_count = len(
            {
                supplier_id
                for item in pending_items
                for supplier_id in item["supplier_ids"]
            }
        )

        submitted = st.form_submit_button(
            f"Create order ({len(pending_items)} item(s), "
            f"{pending_supplier_count} supplier(s))",
            type="primary",
            use_container_width=True,
        )

    if not submitted:
        return

    if not pending_items:
        st.error("No item has an available supplier; cannot create any case.")
        return

    items_missing_suppliers = [
        item["item_material"] for item in pending_items if not item["supplier_ids"]
    ]
    if items_missing_suppliers:
        st.error(
            "Select at least one supplier for: "
            + ", ".join(items_missing_suppliers)
        )
        return

    try:
        case_id = create_case_from_detected_items(
            items=pending_items,
            notes=notes,
            auto_send_messages=auto_send_messages,
            notify_buyer_on_human_review=notify_buyer_on_human_review,
        )

        attachment_errors = _attach_uploaded_files_to_cases(
            uploaded_files, [case_id]
        )

        st.session_state["selected_case_id"] = case_id
        case_created_message = f"Order created: case ID {case_id}."
        if attachment_errors:
            case_created_message += (
                " Some attachments failed to save: "
                + "; ".join(attachment_errors)
            )
        st.session_state["case_created_message"] = case_created_message
        st.rerun()

    except Exception as exc:
        st.error(str(exc))


def _render_manual_case_creation(uploaded_files) -> None:
    """Legacy path: buyer picks one material and its suppliers by hand.

    Used whenever no uploaded file was recognized as an RFQ (or no file was
    uploaded at all) - behavior here is unchanged from before RFQ
    auto-detection was added.
    """
    material_choices = list_material_choices()

    if not material_choices:
        st.warning(
            "No supplier-material catalog is loaded yet. "
            "Run scripts/import_supplier_filter_xlsx.py first."
        )
        return

    material_labels = {
        (
            f"{row['goods_group']} | {row['goods_name']} "
            f"({row['supplier_count']} supplier(s))"
        ): row
        for row in material_choices
    }

    selected_material_label = st.selectbox(
        "Item/material",
        options=list(material_labels.keys()),
        help=(
            "Start typing to search. Only materials imported from the "
            "buyer supplier filter database can be selected."
        ),
        key="new_case_material",
    )

    selected_material = material_labels[selected_material_label]
    selected_goods_name = selected_material["goods_name"]
    suppliers = list_suppliers_for_material(selected_goods_name)

    if not suppliers:
        st.warning(
            "No active suppliers are linked to this material. "
            "Choose another material or re-import the supplier workbook."
        )
        return

    supplier_labels = {
        (
            f"{supplier['name']} | "
            f"{supplier.get('contact_channel') or 'manual'} | "
            f"{supplier.get('email') or supplier.get('whatsapp_number') or 'no contact'}"
        ): supplier["id"]
        for supplier in suppliers
    }

    default_supplier_labels = list(supplier_labels.keys())

    st.caption(
        f"Selected material: {selected_goods_name}. "
        f"Available suppliers from database: {len(default_supplier_labels)}."
    )

    with st.form("create_case_form"):
        quantity = st.number_input(
            "Quantity",
            min_value=0.01,
            value=1.0,
            step=1.0,
        )

        notes = st.text_area("Notes", height=80)

        auto_send_messages = st.checkbox(
            "Send real messages for this case",
            value=False,
            help=(
                "Checked: automatic buyer messages use each supplier's real "
                "email or WhatsApp channel. Unchecked: all outbound messages "
                "stay in the Streamlit chat for simulation."
            ),
        )

        notify_buyer_on_human_review = st.checkbox(
            "Email the buyer when human review is required",
            value=False,
            help=(
                "When checked, each newly created human-review item for this "
                "case sends one internal notification email. The recipient is "
                "BUYER_REVIEW_NOTIFICATION_EMAIL, or BUYER_EMAIL as fallback."
            ),
        )

        selected_supplier_labels = st.multiselect(
            "Suppliers",
            options=list(supplier_labels.keys()),
            default=default_supplier_labels,
            help=(
                "Only suppliers marked with X for the selected material "
                "are shown. You can still uncheck suppliers for this case."
            ),
        )

        submitted = st.form_submit_button(
            "Create case",
            type="primary",
            use_container_width=True,
        )

    if not submitted:
        return

    if not selected_supplier_labels:
        st.error("Select at least one supplier before creating the case.")
        return

    try:
        supplier_ids = [
            supplier_labels[label]
            for label in selected_supplier_labels
        ]

        case_id = create_case(
            item_material=selected_goods_name,
            quantity=quantity,
            notes=notes,
            supplier_ids=supplier_ids,
            auto_send_messages=auto_send_messages,
            notify_buyer_on_human_review=notify_buyer_on_human_review,
        )

        attachment_errors = _attach_uploaded_files_to_cases(uploaded_files, [case_id])

        st.session_state["selected_case_id"] = case_id
        case_created_message = f"Case created successfully: ID {case_id}."
        if attachment_errors:
            case_created_message += (
                " Some attachments failed to save: "
                + "; ".join(attachment_errors)
            )
        st.session_state["case_created_message"] = case_created_message
        st.rerun()

    except Exception as exc:
        st.error(str(exc))


# ---------------------------------------------------------------------
# Item/supplier comparison for an order-case (multiple case_items)
# ---------------------------------------------------------------------

def _build_item_supplier_price_matrix(
    item_rows: list[dict], price_key: str
) -> pd.DataFrame:
    """Pivot the per-item supplier rows from build_item_supplier_overview
    into one case-wide table: one row per item (subcase), one column per
    supplier, so all items can be scanned side by side instead of opening
    a separate table per item."""
    supplier_names: list[str] = []
    seen_suppliers: set[str] = set()

    for item in item_rows:
        for s in item.get("suppliers", []):
            if s["supplier"] not in seen_suppliers:
                seen_suppliers.add(s["supplier"])
                supplier_names.append(s["supplier"])

    index = []
    data = []

    for item in item_rows:
        index.append(f"{item['item_material']} (qty {item['quantity']})")
        prices_by_supplier = {
            s["supplier"]: s[price_key] for s in item.get("suppliers", [])
        }
        data.append(
            [prices_by_supplier.get(name) for name in supplier_names]
        )

    return pd.DataFrame(data, index=index, columns=supplier_names)


def _render_case_wide_price_tables(item_rows: list[dict]) -> None:
    """Two case-wide summary tables (confirmed / provisional prices),
    each with one row per item and one column per supplier - a quick,
    side-by-side comparison instead of one table per item."""

    def _show(title: str, price_key: str) -> None:
        st.markdown(f"**{title}**")
        matrix = _build_item_supplier_price_matrix(item_rows, price_key)

        if matrix.empty:
            st.info("No suppliers linked to any item in this case.")
            return

        styled = matrix.style.format(
            lambda v: "" if pd.isna(v) else f"{v:.2f}"
        ).highlight_min(axis=1, color="#c6efce")

        st.dataframe(styled, use_container_width=True)

    _show("Confirmed prices (USD)", "best_unit_price_usd")
    _show("Provisional prices (USD)", "provisional_unit_price_usd")


def _render_item_supplier_comparison(case_id: int) -> None:
    """One block per order item, comparing the suppliers linked to that
    specific item and letting the buyer notify a winner per item - so
    different items in the same order can go to different suppliers."""
    st.markdown("### Items in this order — supplier comparison")
    st.caption(
        "Each item is compared across the suppliers linked to it. "
        "Different items can be awarded to different suppliers."
    )

    item_rows = build_item_supplier_overview(case_id)

    if not item_rows:
        st.info("No items found for this case.")
        return

    _render_case_wide_price_tables(item_rows)

    st.markdown("#### Item details and winner notification")

    for item in item_rows:
        case_item_id = item["case_item_id"]
        winner = item.get("winner")
        supplier_rows = item.get("suppliers", [])

        header = f"{item['item_material']} (qty {item['quantity']})"
        if winner:
            header += (
                f" — Winner: {winner['supplier_name']} "
                f"(USD {winner['unit_price_usd']})"
            )

        with st.expander(header, expanded=(winner is None)):
            if not supplier_rows:
                st.info("No suppliers linked to this item.")
                continue

            if winner:
                st.success(
                    f"Winner: {winner['supplier_name']} at "
                    f"USD {winner['unit_price_usd']}."
                )
                continue

            best_price = min(
                (
                    s["best_unit_price_usd"]
                    for s in supplier_rows
                    if s["best_unit_price_usd"] is not None
                ),
                default=None,
            )

            for s in supplier_rows:
                has_offer = s["best_unit_price_usd"] is not None
                is_best = (
                    has_offer and s["best_unit_price_usd"] == best_price
                )

                col1, col2 = st.columns([3, 2])

                with col1:
                    prefix = "Best price: " if is_best else ""
                    st.write(f"**{prefix}{s['supplier']}**")
                    if has_offer:
                        st.caption(f"Confirmed: USD {s['best_unit_price_usd']}")
                    elif s.get("provisional_unit_price_usd") is not None:
                        st.caption(
                            f"Provisional: USD "
                            f"{s['provisional_unit_price_usd']}"
                        )
                    else:
                        st.caption("No price yet")

                with col2:
                    if st.button(
                        f"Notify {s['supplier']}",
                        key=(
                            f"notify_item_winner_{case_item_id}_"
                            f"{s['supplier_id']}"
                        ),
                        disabled=not has_offer,
                    ):
                        try:
                            result = (
                                generate_and_send_winner_notification_for_case_item(
                                    case_id=case_id,
                                    case_item_id=case_item_id,
                                    supplier_id=int(s["supplier_id"]),
                                )
                            )

                            send_result = result.get("send_result")
                            outcome = (
                                send_result.get("delivery_outcome")
                                if send_result is not None
                                else None
                            )
                            winner_name = result["winner_supplier"]["name"]

                            if send_result is None or outcome in (
                                "sent",
                                "dry_run",
                            ):
                                st.success(
                                    f"Winner notification sent to "
                                    f"{winner_name} for "
                                    f"{result['item_material']}."
                                )
                            elif outcome == "transient":
                                st.warning(
                                    f"Winner notification for {winner_name} "
                                    "is queued and will retry automatically."
                                )
                            elif outcome == "permanent":
                                st.error(
                                    f"Winner notification for {winner_name} "
                                    "could not be delivered: "
                                    f"{send_result.get('error') or 'permanent failure'}."
                                )
                            elif outcome == "unknown":
                                st.warning(
                                    "Delivery status is unknown for "
                                    f"{winner_name}'s notification after a "
                                    "timeout or connection loss."
                                )
                            else:
                                st.error(
                                    send_result.get("error")
                                    or "Message sending failed."
                                )

                            st.rerun()

                        except Exception as exc:
                            st.error(str(exc))


def _render_supplier_rollup_overview(case_id: int) -> None:
    """Read-only per-supplier rollup across every item they're linked to
    in this order - a quick scan; winner decisions happen in the per-item
    section above, not here."""
    st.markdown("### Supplier overview")
    st.caption(
        "Each supplier's prices across every item they're linked to in "
        "this order. Use the per-item sections above to notify a winner."
    )

    rollup_rows = build_supplier_rollup_overview(case_id)

    if not rollup_rows:
        st.info("No suppliers found for this case.")
        return

    supplier_states = {
        int(row["supplier_id"]): row
        for row in repo.list_supplier_states_for_case(case_id)
    }

    for row in rollup_rows:
        state = supplier_states.get(row["supplier_id"], {}).get(
            "state", "NOT_CONTACTED"
        )
        item_lines = ", ".join(
            f"{item['item_material']}: "
            + (
                f"USD {item['unit_price_usd']}"
                if item["unit_price_usd"] is not None
                else "no price yet"
            )
            for item in row["items"]
        )
        st.markdown(f"**{row['supplier']}** ({state})")
        st.caption(item_lines or "No items linked.")


@st.dialog("Create new negotiation case", width="large")
def show_create_case_dialog() -> None:
    """Render case creation only when the buyer explicitly opens it."""
    uploaded_files = st.file_uploader(
        "Upload an RFQ file (optional)",
        type=["xlsx", "csv", "pdf", "png", "jpg", "jpeg"],
        accept_multiple_files=True,
        help=(
            "Upload a natural-stone or brilliant RFQ spreadsheet and its "
            "item(s) and relevant suppliers are selected automatically, one "
            "case per item. Other files are stored as reference "
            "attachments on the case(s) created below."
        ),
    )

    detection = _merge_rfq_detection(uploaded_files)

    if detection.recognized and detection.items:
        _render_detected_rfq_case_creation(detection, uploaded_files)
        return

    if uploaded_files and not detection.recognized:
        st.caption(
            "No recognized RFQ structure was found in the uploaded "
            "file(s); select the item and suppliers manually below."
        )

    _render_manual_case_creation(uploaded_files)


header_col, create_case_col = st.columns([5, 1])

with header_col:
    st.title("AI Purchase Assistant")

with create_case_col:
    st.write("")
    if st.button(
        "＋ New case",
        type="primary",
        use_container_width=True,
        key="open_create_case_dialog",
    ):
        show_create_case_dialog()

case_created_message = st.session_state.pop("case_created_message", None)
if case_created_message:
    st.success(case_created_message)

cases = list_cases()
if not cases:
    st.info("Create your first case to begin.")
    st.stop()


main_col, selector_col = st.columns([3, 1])


# ---------------------------------------------------------------------
# Right side selectors
# ---------------------------------------------------------------------

with selector_col:
    st.markdown("### Cases")

    case_options = {
        f"{case['case_number']} | {case['item_material']} | {case['status']}": int(case["id"])
        for case in cases
    }

    default_case_id = st.session_state.get("selected_case_id")

    default_case_index = 0
    if default_case_id in case_options.values():
        values = list(case_options.values())
        default_case_index = values.index(default_case_id)

    selected_case_label = st.selectbox(
        "Select case",
        options=list(case_options.keys()),
        index=default_case_index,
    )

    selected_case_id = case_options[selected_case_label]
    st.session_state["selected_case_id"] = selected_case_id

    case_details = repo.get_case_details(selected_case_id)
    case_data = case_details["case"] if case_details else None
    case_suppliers = case_details["suppliers"] if case_details else []

    st.markdown("### Suppliers")

    if not case_suppliers:
        st.warning("No suppliers linked to this case.")
        selected_supplier = None
    else:
        supplier_by_id = {
            int(supplier["id"]): supplier for supplier in case_suppliers
        }

        active_supplier_id = st.session_state.get("selected_supplier_id")
        if active_supplier_id not in supplier_by_id:
            active_supplier_id = int(case_suppliers[0]["id"])
            st.session_state["selected_supplier_id"] = active_supplier_id

        for supplier in case_suppliers:
            supplier_id = int(supplier["id"])
            is_active = supplier_id == active_supplier_id

            if st.button(
                f"{supplier['name']} | {supplier['supplier_code']}",
                key=f"supplier_select_{selected_case_id}_{supplier_id}",
                type="primary" if is_active else "secondary",
                use_container_width=True,
            ):
                if supplier_id != active_supplier_id:
                    st.session_state["selected_supplier_id"] = supplier_id
                    st.rerun()

        selected_supplier = supplier_by_id[active_supplier_id]

    st.markdown("### Communication / automation")

    case_real_mode = bool(case_data and case_data.get("auto_send_messages"))
    st.info(
        "REAL communication" if case_real_mode else "SIMULATION mode"
    )
    st.caption(
        "The mode is stored on the case and applies to RFQs, reminders, "
        "negotiation messages, manual buyer messages, and winner notification."
    )
    st.caption(
        "Human-review email alerts: "
        + (
            "ON"
            if case_data and case_data.get("notify_human_review_email")
            else "OFF"
        )
    )

    auto_refresh_enabled = st.checkbox(
        "Automatically run workflow cycle",
        value=False,
        help=(
            "If enabled, Streamlit periodically checks the mailbox, imports supplier "
            "emails, and lets the system generate the next buyer response."
        ),
    )

    auto_refresh_seconds = st.number_input(
        "Refresh interval seconds",
        min_value=10,
        max_value=300,
        value=30,
        step=10,
    )

    if auto_refresh_enabled:
        st_autorefresh(
            interval=int(auto_refresh_seconds) * 1000,
            key=f"mailbox_autorefresh_case_{selected_case_id}",
        )

    if st.button("Refresh mailbox and continue" if case_real_mode else "Run workflow cycle"):
        try:
            cycle_result = refresh_mailbox_and_continue_case(
                case_id=selected_case_id,
            )

            import_result = cycle_result["import_result"]
            negotiation_result = cycle_result["negotiation_result"]

            st.success(
                f"Imported {import_result['imported_count']} email(s). "
                f"Skipped {import_result['skipped_count']}. "
                f"Created {len(negotiation_result['actions'])} automatic buyer message(s)."
            )

            st.rerun()

        except Exception as exc:
            st.error(str(exc))

    if auto_refresh_enabled:
        try:
            cycle_result = refresh_mailbox_and_continue_case(
                case_id=selected_case_id,
            )

            import_result = cycle_result["import_result"]
            negotiation_result = cycle_result["negotiation_result"]

            if (
                    import_result["imported_count"] > 0
                    or len(negotiation_result["actions"]) > 0
            ):
                st.info(
                    f"Auto-refresh: imported {import_result['imported_count']} email(s), "
                    f"created {len(negotiation_result['actions'])} buyer message(s)."
                )

        except Exception as exc:
            st.error(f"Auto-refresh error: {exc}")


# ---------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------

with main_col:
    if case_data is None:
        st.error("Selected case not found.")
        st.stop()

    st.subheader(f"{case_data['case_number']} — {case_data['item_material']}")
    st.caption(
        f"Quantity: {case_data['quantity']} | "
        f"Status: {case_data['status']}"
    )

    case_items = case_details.get("items") or []
    if case_items:
        with st.expander(
            f"Items in this order ({len(case_items)})",
            expanded=False,
        ):
            st.caption(
                "Each item is negotiated independently across the "
                "supplier(s) linked to it - different items can end up "
                "awarded to different suppliers."
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Item": row["item_material"],
                            "Quantity": row["quantity"],
                            "Suppliers": ", ".join(
                                supplier["name"]
                                for supplier in row.get("suppliers", [])
                            ),
                            "From file": row.get("source_description") or "",
                        }
                        for row in case_items
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

    case_attachments = list_case_attachments(selected_case_id)

    if case_attachments:
        with st.expander(
            f"Attachments ({len(case_attachments)})",
            expanded=False,
        ):
            for attachment in case_attachments:
                size_kb = attachment["size_bytes"] / 1024
                st.markdown(
                    f"**{attachment['original_filename']}** "
                    f"— {size_kb:.1f} KB — "
                    f"{attachment['channel']}/{attachment['direction']} — "
                    f"uploaded {attachment['created_at']}"
                )
    else:
        st.caption("No attachments for this case yet.")

    open_review_items = repo.list_open_human_review_items_for_case(selected_case_id)

    if open_review_items:
        review_count = len(open_review_items)

        with st.expander(
            f"⚠ {review_count} supplier message(s) need buyer review",
            expanded=False,
        ):
            for item in open_review_items:
                review_item_id = int(item["id"])
                supplier_name = item.get("supplier_name") or "Case-level"
                supplier_code = item.get("supplier_code") or ""

                st.markdown(
                    f"### {item['review_type']} — {supplier_name} {supplier_code}"
                )

                st.caption(item["reason"])

                if item.get("message_body"):
                    st.markdown("**Supplier message requiring review:**")
                    st.write(item["message_body"])

                suggestions = build_human_review_suggestions(
                    review_item=item,
                    case_data=case_data,
                )

                show_suggestions = st.checkbox(
                    "Show suggested replies",
                    key=f"show_review_suggestions_{selected_case_id}_{review_item_id}",
                )

                if show_suggestions:
                    for index, suggestion in enumerate(suggestions, start=1):
                        st.markdown(f"**Option {index}: {suggestion['title']}**")
                        st.code(suggestion["body"], language="text")

                        if st.button(
                            f"Send option {index} and resolve",
                            key=f"send_review_option_{selected_case_id}_{review_item_id}_{index}",
                        ):
                            try:
                                result = resolve_human_review_with_reply(
                                    review_item_id=review_item_id,
                                    body=suggestion["body"],
                                )

                                if not result.get("success"):
                                    st.error(result.get("error") or "Message was not sent.")
                                else:
                                    st.success(
                                        "Human review resolved. Buyer reply was created."
                                    )
                                    st.rerun()

                            except Exception as exc:
                                st.error(str(exc))

                        #try:
                        #    result = resolve_human_review_with_reply(
                        #        review_item_id=review_item_id,
                        #        body=suggestion["body"],
                        #    )

                        #    if not result.get("success"):
                        #        st.error(result.get("error") or "Message was not sent.")
                        #    else:
                        #        st.success(
                        #            "Human review resolved. Buyer reply was created."
                        #        )
                        #        st.rerun()

                        #except Exception as exc:
                        #    st.error(str(exc))

                    show_custom_reply = st.checkbox(
                        "Write custom buyer reply",
                        key=f"show_custom_review_reply_{selected_case_id}_{review_item_id}",
                    )

                    if show_custom_reply:
                        custom_body = st.text_area(
                            "Custom reply",
                            height=120,
                            key=f"custom_human_review_reply_{selected_case_id}_{review_item_id}",
                        )

                        if st.button(
                                "Send custom reply and resolve",
                                key=f"send_custom_review_reply_{selected_case_id}_{review_item_id}",
                        ):
                            try:
                                result = resolve_human_review_with_reply(
                                    review_item_id=review_item_id,
                                    body=custom_body,
                                )

                                if not result.get("success"):
                                    st.error(result.get("error") or "Message was not sent.")
                                else:
                                    st.success(
                                        "Human review resolved. Custom buyer reply was created."
                                    )
                                    st.rerun()

                            except Exception as exc:
                                st.error(str(exc))
                    #try:
                    #    result = resolve_human_review_with_reply(
                    #        review_item_id=review_item_id,
                    #        body=custom_body,
                    #    )

                    #    if not result.get("success"):
                    #        st.error(result.get("error") or "Message was not sent.")
                    #    else:
                    #        st.success(
                    #            "Human review resolved. Custom buyer reply was created."
                    #        )
                    #        st.rerun()

                    #except Exception as exc:
                    #    st.error(str(exc))

                show_resolve_without_reply = st.checkbox(
                    "Resolve without sending a reply",
                    key=f"show_resolve_without_reply_{selected_case_id}_{review_item_id}",
                )

                if show_resolve_without_reply:
                    st.caption(
                        "Use this only if the buyer handled the issue outside "
                        "the app or decided no reply is needed."
                    )

                    resolution_note = st.text_area(
                        "Resolution note",
                        height=80,
                        key=f"resolve_review_note_{selected_case_id}_{review_item_id}",
                    )

                    if st.button(
                        "Mark review item resolved",
                        key=f"resolve_review_without_reply_{selected_case_id}_{review_item_id}",
                    ):
                        try:
                            resolve_human_review_without_reply(
                                review_item_id=review_item_id,
                                note=resolution_note,
                            )
                            st.success("Human review item resolved.")
                            st.rerun()

                        except Exception as exc:
                            st.error(str(exc))
                st.markdown("---")

    # -----------------------------------------------------------------
    # Start negotiation
    # -----------------------------------------------------------------

    st.markdown("### Negotiation")

    st.write(
        "Normal workflow: create a case, select suppliers, then press "
        "**Start negotiating**. The system decides which supplier messages "
        "to generate. The buyer only reviews the final supplier overview and "
        "chooses whom to notify as winner."
    )
    if st.button("Start negotiating", type="primary"):
        try:
            result = start_negotiating_case(
                case_id=selected_case_id,
            )

            st.success(
                f"Negotiation action(s) executed: {len(result['actions'])}."
            )

            if result["actions"]:
                st.json(result["actions"])

            st.rerun()

        except Exception as exc:
            st.error(str(exc))

    st.markdown("---")

    # -----------------------------------------------------------------
    # Supplier chat
    # -----------------------------------------------------------------

    if selected_supplier is None:
        st.info("Select a supplier to view chat.")
    else:
        supplier_id = int(selected_supplier["id"])

        st.markdown(f"### Chat with {selected_supplier['name']}")

        messages = repo.list_messages_for_case_supplier(
            case_id=selected_case_id,
            supplier_id=supplier_id,
        )

        if not messages:
            st.info("No messages with this supplier yet.")
        else:
            for msg in messages:
                is_buyer = msg["direction"] == "outbound"

                speaker = "Buyer/system" if is_buyer else selected_supplier["name"]
                status = _describe_outbound_message_status(msg)
                message_type = msg.get("message_type") or "general"
                created_at = msg.get("created_at")

                with st.chat_message("assistant" if is_buyer else "user"):
                    email_info = ""

                    if is_buyer and msg.get("channel") == "email":
                        email_info = f" | to: {selected_supplier.get('email') or 'missing email'}"

                    recipient_info = ""

                    if is_buyer:
                        if msg.get("channel") == "email":
                            recipient_info = f" | to email: {selected_supplier.get('email') or 'missing'}"
                        elif msg.get("channel") == "whatsapp":
                            recipient_info = (
                                f" | to WhatsApp: {selected_supplier.get('whatsapp_number') or 'missing'}"
                            )

                    st.caption(
                        f"{speaker} | {message_type} | {status}{recipient_info} | {created_at}"
                    )


                    st.markdown(msg["body"])

        show_manual_buyer_message = st.checkbox(
            "Write manual buyer message",
            value=False,
            key=f"show_manual_buyer_message_{selected_case_id}_{supplier_id}",
        )

        if show_manual_buyer_message:
            manual_buyer_message = st.text_area(
                "Buyer message",
                height=100,
                key=f"manual_buyer_message_{selected_case_id}_{supplier_id}",
            )

            if st.button(
                    "Send manual buyer message",
                    key=f"send_manual_buyer_message_{selected_case_id}_{supplier_id}",
            ):
                try:
                    result = send_or_display_outbound_message(
                        case_id=selected_case_id,
                        supplier_id=supplier_id,
                        body=manual_buyer_message,
                        message_type="manual_buyer_message",
                    )

                    send_result = result.get("send_result")
                    outcome = (
                        send_result.get("delivery_outcome")
                        if send_result is not None
                        else None
                    )

                    if send_result is None or outcome in ("sent", "dry_run"):
                        st.success("Buyer message sent.")
                    elif outcome == "transient":
                        st.warning(
                            "Message queued. The first delivery attempt failed "
                            "temporarily; the transport worker will retry it "
                            "automatically."
                        )
                    elif outcome == "permanent":
                        st.error(
                            "Message could not be delivered: "
                            f"{send_result.get('error') or 'permanent failure'}. "
                            "See human review."
                        )
                    elif outcome == "unknown":
                        st.warning(
                            "Delivery status is unknown after a timeout or "
                            "connection loss. It was not retried automatically "
                            "to avoid a duplicate send. See human review."
                        )
                    else:
                        st.error(send_result.get("error") or "Message sending failed.")

                    st.rerun()

                except Exception as exc:
                    st.error(str(exc))

        st.markdown("#### Manual supplier response")

        st.write(
            "Use this to simulate or manually enter a supplier reply. "
            "For real email cases, normal replies are imported by the worker; "
            "for WhatsApp, replies arrive through the webhook."
        )

        supplier_body = st.text_area(
            "Supplier response",
            height=120,
            key=f"supplier_response_body_{selected_case_id}_{supplier_id}",
        )

        if st.button("Record supplier response and continue negotiation"):
            try:
                result = record_supplier_message_simple(
                    case_id=selected_case_id,
                    supplier_id=supplier_id,
                    channel="manual",
                    body=supplier_body,
                )
                negotiation_result = continue_negotiation_for_case(
                    case_id=selected_case_id,
                )

                extraction = result["extraction"]

                if result["saved_offer_id"]:
                    item_offers = extraction.get("item_offers")

                    if item_offers:
                        item_summary = ", ".join(
                            f"{item['item_material']}: USD "
                            f"{item['unit_price_usd']} ({item['status']})"
                            for item in item_offers
                        )
                        st.success(
                            f"Supplier response recorded. Price(s) saved "
                            f"for: {item_summary}. Created "
                            f"{len(negotiation_result['actions'])} "
                            "automatic message(s)."
                        )
                    else:
                        offer_status = extraction.get(
                            "offer_status", "confirmed"
                        )

                        if offer_status == "provisional":
                            st.info(
                                f"Supplier response recorded. Provisional "
                                f"price stored: USD "
                                f"{extraction['unit_price_usd']}. It is "
                                "excluded from comparison until confirmed. "
                                f"Created {len(negotiation_result['actions'])} "
                                "automatic message(s)."
                            )
                        else:
                            st.success(
                                f"Supplier response recorded. Confirmed "
                                f"offer saved: USD "
                                f"{extraction['unit_price_usd']}. Created "
                                f"{len(negotiation_result['actions'])} "
                                "automatic message(s)."
                            )
                else:
                    st.info(
                        "Supplier response recorded. "
                        "No confirmed offer was automatically saved."
                    )

                st.rerun()

            except Exception as exc:
                st.error(str(exc))

    st.markdown("---")

    # -----------------------------------------------------------------
    # Supplier overview and winner notification
    # -----------------------------------------------------------------

    if case_items:
        _render_item_supplier_comparison(selected_case_id)
        _render_supplier_rollup_overview(selected_case_id)
        st.stop()

    st.markdown("### Supplier overview")

    overview_rows = build_supplier_overview(selected_case_id)

    if not overview_rows:
        st.info("No suppliers found for this case.")
    else:
        supplier_states = {
            int(row["supplier_id"]): row
            for row in repo.list_supplier_states_for_case(selected_case_id)
        }

        overview_df = pd.DataFrame(
            [
                {
                    "Supplier": row["supplier"],
                    "Code": row["code"],
                    "State": supplier_states.get(
                        int(row["supplier_id"]),
                        {},
                    ).get("state", "NOT_CONTACTED"),
                    "Channel": row["channel"],
                    "Email": row["email"],
                    "Best confirmed price USD": row["best_unit_price_usd"],
                    "Provisional price USD": row["provisional_unit_price_usd"],
                    "Offer status": (
                        "Confirmed"
                        if row["best_unit_price_usd"] is not None
                        else (
                            "Awaiting confirmation"
                            if row["provisional_unit_price_usd"] is not None
                            else "No price"
                        )
                    ),
                    "Confidence": row["best_offer_confidence"],
                }
                for row in overview_rows
            ]
        )

        st.dataframe(
            overview_df,
            use_container_width=True,
            hide_index=True,
        )

    recommendation = get_suggested_winner(selected_case_id)

    recommended_supplier_id = None

    if recommendation is None:
        st.info("No suggested winner yet. Record at least one confirmed offer.")
    else:
        best = recommendation["recommended_offer"]
        recommended_supplier_id = int(best["supplier_id"])

        st.success(
            f"Suggested winner: {best['supplier_name']} "
            f"at USD {best['unit_price_usd']} per unit."
        )

        st.write(recommendation["explanation"])

    st.markdown("#### Notify winner")

    st.write(
        "The buyer chooses the winner here. Pressing a notify button is the "
        "manual final decision."
    )

    if not overview_rows:
        st.info("No suppliers available.")
    else:
        for row in overview_rows:
            supplier_has_offer = row["best_unit_price_usd"] is not None

            label_prefix = "Recommended: " if (
                recommended_supplier_id is not None
                and int(row["supplier_id"]) == recommended_supplier_id
            ) else ""

            col1, col2, col3 = st.columns([3, 2, 2])

            with col1:
                st.write(f"**{label_prefix}{row['supplier']}**")
                st.caption(f"Best price: {row['best_unit_price_usd']}")

            with col2:
                if not supplier_has_offer:
                    provisional_price = row.get("provisional_unit_price_usd")
                    if provisional_price is not None:
                        st.caption(
                            f"Provisional USD {provisional_price}; "
                            "awaiting confirmation"
                        )
                    else:
                        st.caption("No confirmed offer")
                else:
                    st.caption("Confirmed offer available")

            with col3:
                button_disabled = not supplier_has_offer

                if st.button(
                    f"Notify {row['supplier']}",
                    key=f"notify_winner_{selected_case_id}_{row['supplier_id']}",
                    disabled=button_disabled,
                ):
                    try:
                        result = generate_and_send_winner_notification_for_supplier(
                            case_id=selected_case_id,
                            supplier_id=int(row["supplier_id"]),
                        )

                        send_result = result.get("send_result")
                        outcome = (
                            send_result.get("delivery_outcome")
                            if send_result is not None
                            else None
                        )
                        winner_name = result["winner_supplier"]["name"]

                        if send_result is None or outcome in ("sent", "dry_run"):
                            st.success(
                                f"Winner notification sent to {winner_name}."
                            )
                        elif outcome == "transient":
                            st.warning(
                                f"Winner notification for {winner_name} is "
                                "queued. The first delivery attempt failed "
                                "temporarily; the transport worker will retry "
                                "it automatically. The case will move to "
                                "Winner Notified once delivery succeeds."
                            )
                        elif outcome == "permanent":
                            st.error(
                                f"Winner notification for {winner_name} could "
                                f"not be delivered: "
                                f"{send_result.get('error') or 'permanent failure'}. "
                                "See human review."
                            )
                        elif outcome == "unknown":
                            st.warning(
                                f"Delivery status for the winner notification "
                                f"to {winner_name} is unknown after a timeout "
                                "or connection loss. It was not retried "
                                "automatically to avoid a duplicate send. See "
                                "human review."
                            )
                        else:
                            st.error(send_result.get("error") or "Message sending failed.")

                        st.rerun()

                    except Exception as exc:
                        st.error(str(exc))
