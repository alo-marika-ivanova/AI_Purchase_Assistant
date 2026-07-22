# Navrh pravidel pro vyjednavani AI nakupciho

## 1\. Začátek vyjednávacího případu

- Ela vytvoří vyjednávací případ (upload dat)
- AI system vytvoří počáteční nastavení (výběr dodavatelů k notifikaci)
- Ela potvrdí toto nastavení a spustí vyjednávání (klikne na tlačítko)
- AI system pošle RFQ všem vybraným dodavatelům

## 2\. RFQ fáze

- RFQ je odeslán všem vybraným dodavatelům
- RFQ požaduje:
  - Cenu za karát v USD
  - Potvrzení zboží a množství
- AI system čeká na odpovědi

### RFQ čekací doba

Navrhované nastavení:

- Čekáme **1 pracovní den** po odeslání RFQ.
- Pokud nepřijde odpověď, připomeneme se.
- Znovu čekáme
- Pokud nic nepřijde, označíme dodavatele jako **No response**.
- Pokračujeme bez tohoto dodavatele, pokud máme jinak dostatek nabídek.

Testovací nastavení:

- Čekáme **2 minuty** po RFQ.
- Jednou se připomeneme.
- Čekáme další **2 minuty**.
- Označíme jako **No response**.

### Minimální odpovědi pro pokračování

- Máme-li všechny odpovědi, pokračujeme srovnáváním nabídek.
- Pokud nejsou všechny odpovědi, pokračujem pokud buď:
  - Obdrželi jsme alespoň 2 nabídky, nebo
  - Deadline pro RFQ vyprší.
- Pokud je jen 1 nabídka, pokračujeme, ale případ je označen jako **Limited competition**.
- Pokud nemáme nabídku, předáme to Ele.

## 3\. Validace odpovědi

Dodavatelova odpověď je považována za validní, pokud:

- Cena za jednotku je jasně udána
- Měna je v USD nebo tak může být bezpečně interpretována
- Dodavatel je spojen s vvyjednávacím případem
- Zpráva není nejasná

Pokud je odpověď nejasná, AI system pošle jednu zprávu požadující upřesnění.

Příklady vyžadující upřesnění:

- Několik cen v jedné zprávě.
- Dodavatel položí otázku namísto odeslání nabídky
- Dodavatel změní specifikaci požadované položky

Pokud stale nejasné, předáme Ele.

## 4\. Srovnání nabídek

Jakmile shromáždíme validní nabídky:

- Systém určí nejlepší aktuální nabídku
- Systém nastaví cílovou částku jako:

- **Cílová částka = 90% nejlepší nabídkuy**

- Systém uchovává:
  - Nejlepší aktuální nabídku
  - Cílovou částku
  - Nabídky všech dodavatelů

## 5\. Vyjednávací fáze

- Systém vyjednává s jednotlivými dodavateli.
- Systém neodhaluje jména jiných dodavatelů
- Systém může říkat, že má lepší nabídky
- Systém se ptá dodavatelů, zda se mohou dostat na cílovou částku, nebo aspoň blíže.
- Každý dodavatel může obdržet nejvýše **2 vyjednávací zprávy** po obdržení první nabídky
- Systém čeká na odpověď dodavatele, než pošle další vyjednávací zprávu.
- Systém nesmí poslat opakovaně zprávy když čeká na odpověď.

Tón zpráv

- Zdvořilý, přímý, stručný, profesionální, neagresivní, bez zmínění AI

Příklad formulace:

Thank you for the offer. We are still above the level we need for this item. Could you please check whether there is room to get closer to USD X?

## 6\. Reakce dodavatelů během vyjednávání

### Dodavatel souhlasí s cílovou částkou nebo nižší

- Dodavatel se stane kandidátem na vítěze
- Ela potvrdí nebo odmítne.

### Dodavatel vylepší nabídku, ale stale je vyšší než cílová částka

- Systém uloží vylepšenou nabídku.
- Pokud nám zbývají vyjednávací pokusy (neodeslali jsme maximální povolený počet vyjednávacích zpráv) system dale vyjednává směrem k cílové částce.
- Pokud již nejsou vyjednávací pokusy, dodavatelova nejlepší nabídka je uložena pro závěrečné zhodnocení nabídek.

### Dodavatel odmítne nižší cenu

- Systém zaznačí odmítnutí
- Pokud zbývají vyjednávací pokusy, system pošle další vyjednávací zprávu.
- Pokud jsou vyjednávací pokusy vyčerpány, dodaatelova nabídka je uložena pro závěrečné zhodnocení

### Dodavatel neodpoví během vyjednávání

Navrhované nastavení

- Čekáme **1 pracovní den**.
- Pošleme jedno krátké připomenutí
- Čekáme další pracovní den
- Pokud stale nic, dodavatelova poslední nabídka je konečná

Testovací nastavení:

- čekáme **2 minuty**.
- Pošleme připomenutí.
- Čekáme další 2 minuty
- Pokud stale bez odpovědi, použijeme poslední nabídku jako konečnou

## 7\. Když nikdo nepřistoupí na cílovou částku

Pokud nikdo nepřistoupí na cílovou částku po vyčerpání vyjednávacích pokusů:

- Systém srovná konečné nabídky
- Systém vybere nejnižší finální nabídku.
- Pokud je nejnižší finální nabídka v rámci tolerance, přijmeme a navrhneme vítěze
- Jinak dame k řešení Ele

Pravidlo tolerance:

- Pokud je nejnižší Konečná nabídka do **5% nad cílovou částku**, system doporučí její přijetí
- Pokud je nad **5% cílové částky**, dame k řešení Ele

## 8\. Označení vítěze

- Jako vítěz je ozačen:
  - Dodavatel souhlasí s cílovou částkou nebo nižší
  - Dodavatel s nejnižší přijatelnou konečnou nabídkou, nebo
  - Elou manuálně označený dodavatel
- Ne-vítězové nejsou automaticky informováni, pouze pokud to Ela rozhodne

## 9\. Témata, o kterých se nevyjednává (zatím)

- Platební podmínky
- záloha
- cash platba
- zpoždění dodávky
- změna specifikace
- stížnost na kvalitu
- odmítnutí dodávky
- vrácení na náklady dodavatele
- …

Pro tato témata:

- Systém uloží zprávu.
- Systém klasifikuje téma.
- Systém pozastaví automatické vyjadnávání pro tohoto dodavatele.
- Systém vytvoří případ pro zhodnocení Elou
- System může navrhnout odpověď, ale nesmí automaticky odeslat.

## 10\. Neznámý typ konvrezace

Pokud dodavatel pošle zprávu, kterou nelze zařadit do žádné kategorie:

Klasifikujeme jako **Unknown / Needs review**.

- Zastavíme automatické vyjednávání pro dodavatele
- Uložíme konverzaci.
- Notifikujeme Elu.
- Neposíláme automatické odpověfi.

## 11\. Počáteční automatatizace

Bezpečné pro automatické řešení jsou:

- RFQ odeslání
- RFQ připomenutí
- Extrakce ceny
- Srovnání ceny
- Požadavek na snížení ceny
- Ukládání vylepšených nabídek
- Ohodnocení dodavatelů
- Návrh vítěze

Co potřebuje potvrzení Ely:

- Potvrzení nákupu
- Dohodnutí platebních podmínek
- Příjem zálohy
- Diskuse o kvalitě
- Odmítnutí zboží
- Vrácení na náklady dodavatele
- Neznámé typy konverzace
- Akceptování vyšší ceny než toleracne

## 12\. Navrhované hodnoty pro strategii

- RFQ připomenutí po: **1 pracovní den**
- RFQ uzavření bez odpovědi po: **2 pracovní dny**
- Cílová částka: **Nejlepší nabídka mínus 10%**
- Maximální počet vyjednávacích pokusů pro dodavatele: **2**
- Vyjednávací followup po: **1 praconví den**
- Maximální počet AI zpráv bez odpovědi dodavatele:: **2**
- Přijatelná tolerance nad cílovou částku: **5%**
- Notifikace vítěze: **Vyžaduje vždy zásah Ely**

Testování:

- RFQ připomenutí po: **2 min**
- RFQ uzavření bez odpovědi po: **4 min**
- Cílová částka: **Nejlepší nabídka mínus 10%**
- Maximální počet vyjednávacích pokusů pro dodavatele: **2**
- Vyjednávací followup po: **2 min**
- Maximální počet AI zpráv bez odpovědi dodavatele:: **2**
- Přijatelná tolerance nad cílovou částku: **5%**
- Notifikace vítěze: simulace