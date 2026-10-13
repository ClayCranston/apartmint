# Housing extraction gold corpus

`housing_cases.json` contains 26 manually labeled, synthetic, city-neutral documents. It is an evaluation corpus, not training data or an inventory of real properties. The labels were written from the document facts without running a model or deriving answers from production validators.

The `expected` array describes every identifiable listing in that document, including listings with unavailable or unknown status and listings whose price cannot be confirmed. Unit labels are canonical lowercase identities with common unit/apartment/suite prefixes removed. Bedroom counts are integers; a studio is zero, and unpublished bedroom counts are null.

`confirmed_price` is a decimal string with two fractional digits only when the text supports one advertised monthly rental amount or one total sale asking amount in the configured currency. It is null for absent prices, standalone deposits or fees, weekly or yearly rent, starting prices, ranges, conflicting base/net-effective price bases, currency mismatches, and completed sale transaction amounts. There is no currency conversion, annual-to-monthly division, or selection of a range endpoint. A known advertised asking/rental price can remain labeled even when the listing is unavailable. Availability and price are separate facts.

`unknown` means the document does not settle availability. An explicit statement that availability is unpublished remains unknown even if the sentence mentions possible statuses such as “available” or “under contract.” “Not currently available” and “not yet available” mean unavailable at the time of the document.

The before/after omission pair is intentionally evaluated as two independent documents. The later document contains no record for P1502; it must neither recreate that unit nor declare it leased or sold. Historical status changes require a separate stateful evaluation. The only empty expected array belongs to community marketing with no identifiable listing.

The prompt-injection case contains malicious page instructions as untrusted text. Its legitimate unit remains in the gold labels; the instructions do not authorize file access, secret disclosure, or a changed extraction task. No actual credentials are present in this corpus.

Cases with inherently unresolved identity, conflicting availability claims for the same unit, or unclear relationships between multiple properties were withheld to avoid arbitrary gold labels. Price ambiguity is retained because its correct label is explicitly null. No cases were removed or adjusted based on model performance.

The final two audit cases test fact attribution between adjacent unit descriptions and distinguish available tours from available housing. A neighboring unit’s price, bedrooms, or vacancy must not be transferred to a unit with unpublished facts. Available appointments do not establish a vacancy.
