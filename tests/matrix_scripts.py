"""Guion fijo de 10 frases en los cinco idiomas, para make matrix-test.

Mismo contenido semantico en los cinco: asi las medidas de expansion son comparables
entre pares y no reflejan que un guion sea mas largo que otro.

Cada frase lleva al menos un invariante que la traduccion debe preservar: numeros,
fechas, cantidades o nombres propios. Es el guion con el que se evalua el riesgo R1.
"""

from __future__ import annotations

from typing import Final

# Invariantes que deben sobrevivir a la traduccion, en cualquier par.
INVARIANTS: Final[tuple[str, ...]] = (
    "12",
    "2026",
    "3",
    "7",
    "15",
    "2027",
    "9",
    "30",
    "48291",
    "5400",
    "2",
    "28",
    "4",
    "Rodrigo",
    "Arequipa",
    "Lima",
    "Cusco",
    "Mariana",
    "Diego",
)

MATRIX_SCRIPTS: Final[dict[str, list[str]]] = {
    "es": [
        "Buenos días, me llamo Rodrigo y trabajo desde Arequipa.",
        "Las ventas subieron 12 por ciento en el tercer trimestre de 2026.",
        "El equipo de Lima cerró 3 contratos y el de Cusco cerró 7.",
        "Necesitamos el presupuesto aprobado antes del 15 de marzo de 2027.",
        "Mariana presentará el informe el lunes a las 9 y 30 de la mañana.",
        "El número de factura es 48291 y el importe es 5400 dólares.",
        "Nuestra oficina de São Paulo abrió el 2 de junio del año pasado.",
        "Por favor confirma con Diego antes del viernes 28.",
        "El proyecto tiene 4 fases y estamos en la segunda.",
        "Muchas gracias por su tiempo y nos vemos la próxima semana.",
    ],
    "en": [
        "Good morning, my name is Rodrigo and I work from Arequipa.",
        "Sales rose 12 percent in the third quarter of 2026.",
        "The Lima team closed 3 contracts and Cusco closed 7.",
        "We need the budget approved before March 15, 2027.",
        "Mariana will present the report on Monday at 9 30 in the morning.",
        "The invoice number is 48291 and the amount is 5400 dollars.",
        "Our São Paulo office opened on June 2 of last year.",
        "Please confirm with Diego before Friday the 28th.",
        "The project has 4 phases and we are in the second one.",
        "Thank you very much for your time and see you next week.",
    ],
    "pt-BR": [
        "Bom dia, meu nome é Rodrigo e eu trabalho de Arequipa.",
        "As vendas subiram 12 por cento no terceiro trimestre de 2026.",
        "A equipe de Lima fechou 3 contratos e a de Cusco fechou 7.",
        "Precisamos do orçamento aprovado antes de 15 de março de 2027.",
        "Mariana vai apresentar o relatório na segunda-feira às 9 e 30 da manhã.",
        "O número da fatura é 48291 e o valor é 5400 dólares.",
        "Nosso escritório de São Paulo abriu no dia 2 de junho do ano passado.",
        "Por favor confirme com Diego antes de sexta-feira, dia 28.",
        "O projeto tem 4 fases e estamos na segunda.",
        "Muito obrigado pelo seu tempo e até a próxima semana.",
    ],
    "fr": [
        "Bonjour, je m'appelle Rodrigo et je travaille depuis Arequipa.",
        "Les ventes ont augmenté de 12 pour cent au troisième trimestre 2026.",
        "L'équipe de Lima a signé 3 contrats et celle de Cusco en a signé 7.",
        "Nous avons besoin du budget approuvé avant le 15 mars 2027.",
        "Mariana présentera le rapport lundi à 9 heures 30 du matin.",
        "Le numéro de facture est 48291 et le montant est de 5400 dollars.",
        "Notre bureau de São Paulo a ouvert le 2 juin de l'année dernière.",
        "Merci de confirmer avec Diego avant vendredi 28.",
        "Le projet compte 4 phases et nous en sommes à la deuxième.",
        "Merci beaucoup pour votre temps et à la semaine prochaine.",
    ],
    "ja": [
        "おはようございます。私の名前はロドリゴで、アレキパから働いています。",
        "2026年の第3四半期に売上が12パーセント上がりました。",
        "リマのチームは3件の契約を締結し、クスコは7件でした。",
        "2027年3月15日までに予算の承認が必要です。",
        "マリアナが月曜日の朝9時30分に報告書を発表します。",
        "請求書番号は48291で、金額は5400ドルです。",
        "サンパウロの事務所は昨年の6月2日に開設しました。",
        "金曜日の28日までにディエゴに確認してください。",
        "このプロジェクトは4つの段階があり、今は第2段階です。",
        "お時間をいただきありがとうございました。また来週お会いしましょう。",
    ],
}

# Voz de Fish por idioma fuente. La voz por defecto es inglesa y arrastra acento en los
# demas idiomas, lo que degrada el STT del fixture y contaminaria la medida.
SOURCE_VOICES: Final[dict[str, str | None]] = {
    "es": "dfa5b230c8054f429e434f4a6e9bbdec",  # Farid Dieck
    "en": "802e3bc2b27e49c2995d23ef70e6ac89",  # Energetic Male
    "pt-BR": "04736e4d6a644abab81e601a7d2ae4b9",  # Waldo Morais
    "fr": "a288bdc744da4ad194921adad6863175",  # Clemence
    "ja": "0089dce5fefb4c6ba9b9f2f0debe1ddc",  # ochitsuita josei
}
