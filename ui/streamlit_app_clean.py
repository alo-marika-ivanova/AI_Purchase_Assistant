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
from app.services.case_service import create_case, list_cases
from app.services.supplier_catalog_service import (
    list_material_choices,
    list_suppliers_for_material,
)
from app.services.simple_chat_service import (
    build_supplier_overview,
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


# ---------------------------------------------------------------------
# Case creation dialog
# ---------------------------------------------------------------------

@st.dialog("Create new negotiation case", width="large")
def show_create_case_dialog() -> None:
    """Render case creation only when the buyer explicitly opens it."""
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

        st.session_state["selected_case_id"] = case_id
        st.session_state["case_created_message"] = (
            f"Case created successfully: ID {case_id}."
        )
        st.rerun()

    except Exception as exc:
        st.error(str(exc))


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
        supplier_options = {
            f"{supplier['name']} | {supplier['supplier_code']}": supplier
            for supplier in case_suppliers
        }

        selected_supplier_label = st.selectbox(
            "Select supplier",
            options=list(supplier_options.keys()),
        )

        selected_supplier = supplier_options[selected_supplier_label]

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
                status = msg.get("status")
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

                    if send_result is not None and not send_result.get("success"):
                        st.error(send_result.get("error") or "Message sending failed.")
                    else:
                        st.success("Buyer message created.")
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
                    st.success(
                        f"Supplier response recorded. "
                        f"Offer saved: USD {extraction['unit_price_usd']}. "
                        f"Created {len(negotiation_result['actions'])} automatic message(s)."
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
                    "Best unit price USD": row["best_unit_price_usd"],
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

                        if send_result and not send_result.get("success"):
                            st.error(send_result.get("error"))
                        else:
                            st.success(
                                f"Winner notification generated for "
                                f"{result['winner_supplier']['name']}."
                            )

                        st.rerun()

                    except Exception as exc:
                        st.error(str(exc))