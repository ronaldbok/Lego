# LEGO onderdelenlijst (Windows)

Een eerste desktopversie voor Windows. De app opent de officiële LEGO-pagina voor een set, leest een lokaal gedownloade instructie-PDF en kan onderdelen aanvullen met namen en kleuren uit Rebrickable.

## Starten

1. Installeer Python 3.10 of nieuwer.
2. Open PowerShell in deze map en voer uit:

   ```powershell
   py -m pip install -r requirements.txt
   py lego_parts_app.py
   ```

3. Vul het setnummer in en druk op Enter of klik op **Set openen en analyseren**. De app laadt de officiële PDF's, controleert de hoofdindex en berekent de bouwstappen per onderdeel.
4. De app bewaart de PDF's, hoofdindex en bouwstapanalyse in `downloads\<setnummer>` en hergebruikt ze bij het opnieuw openen. Wijzigingen aan de handleiding of herkenning worden opnieuw verwerkt.
5. Vul een setnummer in en kies **Onderdelen details… → Setbeheer → Open offline bouwstappen…** om een eerder opgeslagen overzicht zonder openstaande verschillen te openen. Gebruik **Verschil verklaren…** in **Bouwstappen per onderdeel…** om een bekende afwijking met reden vast te leggen.
6. Klik **Visueel vergelijken…** om een PDF-element naast een Rebrickable-onderdeelafbeelding te bekijken. Selecteer aan beide kanten een onderdeel en bevestig een match alleen als vorm en kleur overeenkomen. Bevestigde koppelingen worden per set lokaal opgeslagen.
7. Kies **Onderdelen details… → Setbeheer → Exporteer onderdelen naar excel** om de onderdelenlijst als `.xlsx`-werkmap op te slaan. Standaard wordt de export in `downloads\<setnummer>` voorgesteld.

Via **Onderdelen details…** open je het setbeheer en de instellingen. In **Setbeheer** vind je ook **Bouwstappen bekijken…** en **Bouwstappen per onderdeel…**. Voor omschrijvingen kun je een persoonlijke [Rebrickable API-sleutel](https://rebrickable.com/api/) opslaan onder **Instellingen**. Daar stel je ook de klantnaam voor het huurformulier in.

De app bewaart geleerde onderdeelherkenningen en elementkoppelingen apart in `herkenningskennis.sqlite3`. Met **Set verwijderen…** verwijder je de lokale setbestanden. Bij een latere analyse kan de app dezelfde gecontroleerde tekeningen en elementkoppelingen opnieuw toepassen.

Onder **Eigen LEGO-sets…** registreer je sets per eigenaar en aantal. De setnaam, het aantal onderdelen, het thema en de leeftijdsindicatie komen van LEGO.com. Onder **Gehuurde LEGO-sets…** vind je diezelfde gegevens voor de Superbouwers-huurgeschiedenis. Een registratie in een van beide lijsten start ook de gewone setanalyse. Beide lijsten blijven beschikbaar als je gedownloade setgegevens verwijdert.

Selecteer een gehuurde set om onderdeelafwijkingen vast te leggen of het A4-onderdelenformulier te maken en af te drukken. **Retourzending aanmaken…** opent het retourportaal van Superbouwers.

## Eerste versie

De automatische download haalt de officiële LEGO PDF-links eerst van de LEGO-instructiepagina en vult die aan met LEGO's GraphQL-instructiegegevens. LEGO documenteert deze interne API niet als publieke ontwikkelaars-API; als LEGO de pagina of API wijzigt, kan de downloadfunctie aanpassing nodig hebben. Installeer na wijzigingen in `requirements.txt` de vereisten opnieuw met `py -m pip install -r requirements.txt`.
