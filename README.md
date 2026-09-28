# 718 Made - Weidian shop monitor

Checkt elke 10 minuten de Weidian-shop van 718 Made en post alleen de
wijzigingen naar Discord, als nette embeds met foto, prijs en voorraad.

Gemeld worden:

| Wijziging | Kleur |
|---|---|
| Nieuw product | groen |
| Weer op voorraad | blauw |
| Uitverkocht | rood |
| Prijswijziging | oranje (met oude prijs doorgestreept) |
| Product verwijderd | grijs |

## Productnamen

Rauwe Weidian-namen zijn rommelig: `718制造 重磅卫衣 灰色 包邮` of
`PRD 25SS Canvas 双膝伐木裤/正品riri拉链/山羊皮`. Het script laat Claude die
niet alleen vertalen maar ook opschonen en het merk herkennen:

| Rauw | Wordt |
|---|---|
| `718制造 重磅卫衣 灰色 包邮` | Balenciaga Heavyweight Sweatshirt - Grey |
| `PRD 25SS Canvas 双膝伐木裤/...` | Prada Canvas Carpenter Pants - Navy |
| `GC La Famiglia Introverso 锁扣...` | Gucci La Famiglia Jeans - Washed Grey |

Merkafkortingen (PRD, GC, KS, MM6, CG, BLCG) en Chinese merknamen worden
uitgeschreven. Verkopersruis zoals 包邮 (gratis verzending) en spec-dumps
worden weggelaten.

`products.json` bevat de producten die 718 Made maakt, met een `hints`-veld
per product. Hoe voller die hints, hoe beter de herkenning. Zet er gerust
Chinese termen, kleurnamen, modelnamen en gewichten in.

Is het merk niet af te leiden, dan gebruikt het script alleen het producttype.
Het verzint nooit een merk en gebruikt 718 Made nooit als merknaam.

De originele Chinese naam blijft in de embed onder de titel staan, zodat je het
product in de shop kunt terugvinden. Verwerkte namen worden bewaard in
`state.json`, dus elke naam gaat maar een keer door de API.

## Hoe het werkt

1. GitHub Actions draait elke 10 minuten.
2. Het script haalt de volledige productlijst van de shop op.
3. Die wordt vergeleken met de momentopname in `state.json`.
4. Alleen verschillen worden als embeds naar de Discord webhook gestuurd
   (maximaal 10 per bericht, de rest volgt in vervolgberichten).
5. De nieuwe momentopname wordt teruggecommit naar de repo.

De eerste run post niets: die legt alleen de beginsituatie vast. Anders zou de
hele catalogus in een keer als "nieuw product" in het kanaal belanden.

## Setup

1. Maak een Discord webhook aan in het doelkanaal.
2. Zet in de repo deze secrets (Settings -> Secrets and variables -> Actions):

| Naam | Waarde |
|---|---|
| `DISCORD_WEBHOOK_URL` | de webhook van het kanaal |
| `ANTHROPIC_API_KEY` | dezelfde key als de Weibo-bots |
| `WEIDIAN_COOKIE` | alleen nodig als Weidian zonder login niets teruggeeft |

3. Draai de workflow handmatig. De eerste run legt de momentopname vast.

## Het endpoint

Het script gebruikt het verzoek dat de shoppagina zelf doet:

```
https://thor.weidian.com/decorate/shopDetail.tab.getItemList/1.0?param={"shopId":"1679474173","tabId":0,"sortOrder":"desc","offset":0,"limit":20,"from":"h5","showItemTag":true}
```

Het loopt met `offset` in stappen van 20 door de hele catalogus.

Belangrijk: Weidian eist een **Referer-header** die naar de shop wijst. Open je
deze URL rechtstreeks in je browser, dan krijg je `Referer Not Allowed` - dat is
normaal en betekent niet dat het endpoint kapot is. Het script stuurt die header
wel mee.

Gebruikte velden per product: `itemId`, `itemName`, `price`, `stock`, `sold`,
`itemImg`, `itemUrl`.

### Als Weidian het endpoint wijzigt

Zie je in het log "Geen producten gevonden", haal dan het nieuwe verzoek op:

1. Open de shop in Chrome: `https://shop1679474173.v.weidian.com/`
2. F12 -> tabblad **Network** -> klik op het filter **Fetch/XHR**
3. Ververs de pagina en scroll naar beneden tot alle producten geladen zijn
4. Klik de verzoeken in de lijst langs en kijk bij **Preview** welk verzoek de
   productnamen en prijzen teruggeeft
5. Kopieer van dat verzoek de volledige **Request URL**

Zet die URL in de workflow bij `WEIDIAN_API_URL` en vervang het shop-id door
`{shop_id}` en de paginering door `{offset}` en `{limit}`.

Let op: plak nooit je cookies in de workflow of in een chat. Die horen alleen
in het `WEIDIAN_COOKIE` secret.

## Aandachtspunten

- **Onbeperkte voorraad.** Weidian gebruikt extreem hoge getallen (zoals
  999999999) om "onbeperkt" aan te geven. Die worden als "Unlimited" getoond.
  Zulke producten zullen dus nooit als uitverkocht gemeld worden.
- **Ongedocumenteerde API.** Net als bij de Weibo-bots kan Weidian de
  structuur wijzigen. Het script logt dan wat het wel binnenkreeg.
- **Prijzen in CNY**, zoals Weidian ze teruggeeft.
