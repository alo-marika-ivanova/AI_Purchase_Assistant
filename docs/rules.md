# AI Purchasing Agent Negotiation Rules

## 1. Starting a Negotiation Case

* Ela creates a negotiation case by uploading or entering the purchasing data.
* The system prepares the initial setup, including the suppliers to be contacted.
* Ela reviews and confirms the setup, then starts the negotiation.
* The system sends an RFQ to all selected suppliers.

## 2. RFQ Stage

* The RFQ is sent to all selected suppliers.
* The RFQ requests:

  * the unit price per carat in USD;
  * confirmation of the requested item and quantity.
* The system waits for supplier responses.

### RFQ Waiting Period

Production settings:

* Wait **1 business day** after sending the RFQ.
* If no response is received, send a reminder.
* Wait again.
* If there is still no response, mark the supplier as **No Response**.
* Continue without that supplier if enough other offers are available.

Testing settings:

* Wait **2 minutes** after sending the RFQ.
* Send one reminder.
* Wait another **2 minutes**.
* Mark the supplier as **No Response**.

### Minimum Responses Required to Continue

* If all suppliers have responded, continue to offer comparison.
* If not all suppliers have responded, continue when either:

  * at least two valid offers have been received; or
  * the RFQ deadline has expired.
* If only one valid offer is available, continue but mark the case as **Limited Competition**.
* If no valid offer is available, escalate the case to Ela.

## 3. Response Validation

A supplier response is considered a valid offer when:

* a clear unit price is provided;
* the currency is USD or can be interpreted safely as USD;
* the supplier is associated with the negotiation case;
* the message is not materially ambiguous.

If the response is unclear, the system sends one message requesting clarification.

Examples requiring clarification include:

* several prices in one message without a clear distinction;
* a total price without a clear unit price;
* missing or unclear currency;
* the supplier asking a question instead of providing an offer;
* the supplier changing the requested item specification;
* a conditional price whose conditions are unclear.

If the response remains unclear after one clarification attempt, escalate it to Ela.

## 4. Provisional Supplier Prices

A price is provisional when the supplier indicates that it is tentative, estimated, subject to confirmation, or still being checked.

Examples include:

* “I am almost sure the price is USD 20.”
* “The price should be around USD 20, but I need to confirm.”
* “I will check with my supervisor, but it is probably USD 20.”

When a provisional price is received:

1. Store it as a **provisional offer**.
2. Preserve the original supplier message.
3. Exclude the provisional offer from:

   * the minimum valid-offer count;
   * offer comparison;
   * supplier ranking;
   * target-price calculation;
   * winner selection.
4. Send one short acknowledgement and wait for confirmation.
5. Do not repeatedly acknowledge or follow up while waiting for the supplier.
6. A contextual confirmation such as “Confirmed” or “Yes, confirmed” confirms the stored provisional price.
7. A later explicit confirmed price supersedes the provisional price.
8. The confirmed price becomes an active offer and may be used in comparison and negotiation.
9. The earlier provisional record should remain available for history or be marked as superseded.
10. If the supplier’s confirmation remains unclear, request one clarification and then escalate to Ela if necessary.

A provisional offer must not be treated as **No Offer** while the system is actively waiting for confirmation. However, if the applicable deadline expires without confirmation and no other valid offer exists, the normal no-valid-offer escalation rules apply.

## 5. Initial Offer Comparison

Once valid confirmed offers have been collected:

* The system identifies the current best offer.
* The system calculates the target price as:

**Target price = 90% of the current best offer**

The system stores:

* the current best offer;
* the target price;
* all confirmed supplier offers;
* the current ranking.

Provisional offers must not be included in these calculations.

## 6. Negotiation Stage

* The system negotiates separately with each supplier.
* The system must not reveal the names of competing suppliers.
* The system may state that better market offers are available.
* The system asks whether the supplier can reach the target price or move closer to it.
* Each supplier may receive no more than **two negotiation messages** after submitting the first valid confirmed offer.
* The system must wait for the supplier’s response before sending another negotiation message.
* The system must not repeatedly send messages while waiting for a response.

### Message Tone

Messages must be:

* polite;
* direct;
* concise;
* professional;
* non-aggressive;
* written without mentioning AI or automation.

Example:

> Thank you for the offer. We are still above the level we need for this item. Could you please check whether there is room to get closer to USD X?

## 7. Supplier Responses During Negotiation

### Supplier Accepts the Target Price or a Lower Price

* The supplier becomes a candidate for winner selection.
* Ela confirms or rejects the proposed winner.

### Supplier Improves the Offer but Remains Above the Target

* Store the improved offer.
* If negotiation attempts remain, continue negotiating toward the target price.
* If no attempts remain, store the supplier’s best offer for final evaluation.

### Supplier Refuses to Reduce the Price

* Record the refusal.
* If negotiation attempts remain, the system may send another negotiation message.
* If all attempts have been used, store the supplier’s best offer for final evaluation.

### Supplier Does Not Respond During Negotiation

Production settings:

* Wait **1 business day**.
* Send one short reminder.
* Wait another business day.
* If there is still no response, use the supplier’s latest valid confirmed offer as the final offer.

Testing settings:

* Wait **2 minutes**.
* Send one reminder.
* Wait another **2 minutes**.
* If there is still no response, use the latest valid confirmed offer as the final offer.

The system must not send another negotiation message before the supplier replies or the applicable reminder deadline is reached.

## 8. When No Supplier Accepts the Target Price

If no supplier accepts the target price after all negotiation attempts have been exhausted:

* Compare all final confirmed offers.
* Identify the lowest final offer.
* If the lowest final offer is within the permitted tolerance, recommend accepting it and propose the supplier as the winner.
* Otherwise, escalate the decision to Ela.

### Tolerance Rule

* If the lowest final offer is no more than **5% above the target price**, recommend acceptance.
* If it is more than **5% above the target price**, escalate the case to Ela.

## 9. Winner Selection

A supplier may be selected as the winner when:

* the supplier accepts the target price or a lower price;
* the supplier has the lowest acceptable final offer; or
* Ela manually selects the supplier.

Winner notification always requires Ela’s approval.

Non-winning suppliers are not automatically informed unless Ela explicitly decides to notify them.

## 10. Topics Not Negotiated Automatically

The system does not currently negotiate or decide matters concerning:

* payment terms;
* deposits;
* cash payments;
* delivery delays;
* changes to the requested specification;
* quality complaints;
* rejection of delivered goods;
* returns at the supplier’s expense;
* legal or compliance matters;
* exclusivity;
* disputes;
* other non-price commercial terms.

For these topics:

1. Store the supplier’s message.
2. Classify the topic.
3. Pause automatic negotiation for that supplier.
4. Create a human-review item for Ela.
5. The system may suggest a draft reply.
6. The system must not send the reply automatically.

## 11. Unknown Conversation Type

If a supplier sends a message that cannot be classified reliably, classify it as:

**Unknown / Needs Review**

Then:

* pause automatic negotiation for the supplier;
* store the full conversation;
* notify Ela;
* create a human-review item;
* do not send an automatic response.

## 12. Initial Automation Scope

The following actions are considered safe for automatic processing:

* sending RFQs;
* sending RFQ reminders;
* extracting prices;
* validating prices;
* storing provisional offers;
* acknowledging provisional offers once;
* converting clearly confirmed provisional offers into confirmed offers;
* comparing confirmed prices;
* requesting a price reduction;
* storing improved offers;
* evaluating and ranking suppliers;
* proposing a winner.

The following actions require Ela’s confirmation or intervention:

* confirming the purchase;
* notifying the winner;
* agreeing to payment terms;
* accepting or requesting a deposit;
* discussing quality matters;
* rejecting goods;
* arranging returns at the supplier’s expense;
* handling unknown conversation types;
* accepting a price above the permitted tolerance;
* handling unusual or unsupported commercial topics.

## 13. Recommended Strategy Values

### Production Settings

* RFQ reminder after: **1 business day**
* RFQ closed without response after: **2 business days**
* Target price: **10% below the best confirmed offer**
* Maximum negotiation messages per supplier: **2**
* Negotiation follow-up after: **1 business day**
* Maximum AI messages without a supplier response: **2**
* Acceptable tolerance above target price: **5%**
* Winner notification: **Always requires Ela’s action**

### Testing Settings

* RFQ reminder after: **2 minutes**
* RFQ closed without response after: **4 minutes**
* Target price: **10% below the best confirmed offer**
* Maximum negotiation messages per supplier: **2**
* Negotiation follow-up after: **2 minutes**
* Maximum AI messages without a supplier response: **2**
* Acceptable tolerance above target price: **5%**
* Winner notification: **Simulated or manually confirmed**
