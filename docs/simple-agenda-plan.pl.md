# Plan prostej agendy Librusa

Zatwierdzony plan jednego silnika agendy. Szczegóły przyrostowego wejścia i checkpointu opisuje [kontrakt przyrostowy](incremental-agenda-contract.md).
Cel: jeden plan z tekstów trzech źródeł, jedna agenda i obecny bezpieczny wykonawca Calendar.
Istniejące wpisy i decyzje rodzica są stanem wejściowym, który trzeba zachować.

## Zakres i zasady

- Czytamy tekst wiadomości, ogłoszeń i kalendarza szkolnego dla wybranego ucznia.
  Zachowujemy identyfikator, wersję i kontekst źródła; błąd pobrania nie oznacza pustego źródła.
- Nie pobieramy treści załączników, obrazów, PDF, OCR ani stron z linków.
  Panel może nadal oznaczać nieprzeczytany załącznik.
- Zostają: jedna baza SQLite, obecny runtime, docelowy Calendar, harmonogram aplikacji,
  panel decyzji, dziennik operacji, blokada jednego workera i monitoring Netdata.
- Nie uruchamiamy kolejnej godzinowej automatyzacji akceptacyjnej Codex.
  Godzinowa synchronizacja aplikacji nadal jest częścią docelowego działania.

## Docelowy przepływ

1. Worker pod obecną blokadą odzyskuje przerwane operacje i rozlicza nieznane wyniki
   wyłącznie przez odczyt. Obsługuje zapisane decyzje użytkownika i zbiera źródła.
2. Odczytuje aktualne zarządzane wpisy Calendar. Buduje spójny snapshot: pełna aktualna
   agenda, pełne nowe lub zmienione teksty, teksty potrzebne po zmianie decyzji lub Calendar, zwarty katalog wszystkich źródeł, zachowane fakty przyszłych wpisów,
   powiązania źródeł, decyzje, wybory ofert oraz zweryfikowany kontekst ucznia.
   Źródło spoza okna, które nadal uzasadnia aktywny wpis, pozostaje istotne.
3. Mierzy rozmiar wejścia względem budżetu kontekstu wybranego modelu, z miejscem na wynik.
   Wysyła pełną agendę, decyzje i oferty oraz wybrany przyrost tekstów; nie analizuje ponownie dawnej pominiętej historii. Przekroczenie budżetu daje widoczny
   błąd przed wywołaniem; bez cichego obcinania i własnego silnika stronicowania.
4. Jeden tool-free task na jawnie skonfigurowanym `gpt-6.1-sol/high` zwraca plan całej zmiany:
   `create`, `link`, `update`, `review`, `ignore`, z uzasadnieniem i wskazaniem źródeł.
   `link` dołącza kolejne źródło do istniejącej czynności bez zapisu Calendar.
   Model widzi również odrzucenia i wybrane terminy, żeby nie proponować ich ponownie.
5. Kod waliduje schemat, daty, istniejące referencje, zakres ucznia, własność, decyzje
   oraz sprzeczne/podwójne operacje w jednym planie. Nadaje stabilne ID i zapisuje plan
   w tej samej bazie przed przygotowaniem operacji; zapis i kolejka są transakcyjne.
   Błędny schemat lub obce referencje blokują niebezpieczny plan. Poprawne pozycje review
   nie blokują niezależnych jasnych obowiązków; niepewny potencjalny duplikat trafia do review,
   bez pomijania go w ciemno. Zależne wybory jednej oferty pozostają atomowe.
6. Zaakceptowany plan trafia do obecnego dziennika i wykonawcy. Wykonawca ponownie
   sprawdza własność i aktualny fingerprint przed zapisem; nieznany wynik wymaga
   odczytu i uzgodnienia, nigdy ponownego create w ciemno.

Hash do ponownego użycia planu obejmuje treści/wersje źródeł, agendę i aktualny stan
zarządzanych wpisów Calendar, decyzje użytkownika, kontekst ucznia, politykę,
wersję planera/promptu i konfigurację modelu. Ponowna kontrola przed kolejką odrzuca
plan, jeśli stan zmienił się podczas inference. Sam nowy czas pobrania lub zmiana dnia nie jest zmianą treści. Lokalny CAS obejmuje pełny stan bez limitu bajtów; hash rzeczywistego żądania jest osobny. Dodatkowy rejestr wersji źródeł uwzględnia treść, usunięcie, retencję i dostępność tekstu; checkpoint tylko jawnie obsłużonych wersji jest atomowy z materializacją planu. Sam katalog lub dawny tekst kontekstowy nie uprawnia do nowego automatycznego wpisu.
Udane własne zapisy aktualizują baseline/checkpoint, więc ich zmiany agendy i Calendar
oraz techniczne timestampy nie uruchamiają inference ponownie; zmiany zewnętrzne nadal je unieważniają.
Brak zmiany wejścia pomija inference; worker nadal rozlicza operacje, wykonuje zatwierdzone
decyzje i aktualizuje health. Nie tworzy nowego zadania modelu dla każdego kandydata.

## Decyzje i ochrona danych

- Zwykłe obowiązki szkolne z jednoznacznym terminem mogą trafić automatycznie do Calendar.
  Oferta pozaszkolna wymaga zgody na udział; niejasna data, odbiorca lub konflikt wymaga review.
- Daty, zadania całodniowe i dokładne godziny zachowują obecny kontrakt Europe/Warsaw.
  Model nie zastępuje godzin zatwierdzonych przez człowieka domyślną długością wydarzenia.
- Odrzucenia, poprawki rodzica i zamknięte wybory ofert są nadrzędne wobec nowego planu.
  Oferta „jeden z pięciu terminów” pozostaje jedną grupą; wybór kolejnego terminu
  wymaga nowej decyzji, a pozostałe nie tworzą osobnych wpisów.
- Istotna zmiana wpisu zatwierdzonego lub ręcznie zmienionego przez użytkownika wymaga
  ponownej zgody. Nie wolno automatycznie przyjąć nowego fingerprintu za zgodę na nadpisanie.
- Anulowanie zawsze wymaga jawnego potwierdzenia istniejącą ścieżką decyzji.
  Brak źródła, brak wpisu w wyniku modelu lub pusta lista nie oznaczają usunięcia.
- Preview zapisuje tylko izolowany wynik podglądu, bez zmian aktywnych decyzji,
  grup, kolejki operacji i Calendar. Akceptacja wymaga ponownej kontroli aktualności.
- Zachowujemy obecną retencję prywatnych treści i minimalny trwały stan decyzji,
  własności oraz nierozliczonych operacji; nie gromadzimy bezterminowo pełnych promptów.

## Mapa zmian i ponowne użycie

| Obszar | Konkretna zmiana |
| --- | --- |
| `app/librus_client.py`, `app/source_collectors.py` | Zachować pobieranie tekstu i sprawdzanie kontekstu/kompletności; bez nowych źródeł. |
| `app/source_store.py`, `app/core.py` | Oprzeć agendę na obecnych `canonical_actions`, powiązaniach i decyzjach; dodać tylko niezbędny zapis planu/hash. |
| `app/codex_runtime.py`, `app/schemas/` | Jeden interfejs planowania snapshotu i jeden schemat wyniku; zachować obecne odczyty i wykonanie Calendar. |
| `app/sync.py` | Jeden aktywny przebieg planowania obejmujący także wiadomości; usunąć niezależne tworzenie przez dawny analizator wiadomości. |
| `app/source_planner.py`, `app/source_matching.py` | Zastąpić łańcuch ekstrakcji, matchingu, recovery i weryfikacji prostym planerem; nie dokładać drugiej stałej warstwy. |
| `app/source_preview.py`, `app/source_admin.py` | Po akceptacji usunąć tymczasowy przełącznik silników i stare aktywne ścieżki; zachować użyteczny podgląd tylko do odczytu. |
| `app/offer_groups.py`, `app/web.py` | Zachować ID grup i decyzje; pokazywać jedną czynność z kilkoma źródłami i prostym uzasadnieniem. |
| `app/health_state.py`, `app/monitoring.py` | Zachować kontrakt health i prywatność; uwzględnić stan planowania bez nowej platformy. |
| `app/CONTRACT.md`, dokumentacja architektury | Zaktualizować do jednej aktywnej ścieżki po implementacji. |

Migracja jest idempotentnym dołączeniem obecnych wpisów do agendy przez istniejące
mapowania `events` → `proposals` → `canonical_actions`/`action_sources`.
Zachowuje ID Calendar, propozycji, grup i operacji, markery, decision intents oraz historię.
Jeśli rekord kanoniczny już istnieje, używa go. Brak jednoznacznego mapowania daje review,
bez naprawiania hurtowo starych pól semantycznych. Nie resetuje bazy, nie usuwa ani
odtwarza wpisów Calendar i nie przelicza całej historycznej skrzynki jako nowych spraw.

## Kolejność wykonania i akceptacja

1. **POC plan-only:** w prywatnym chronionym środowisku najpierw zebrać świeży snapshot
   źródeł, zarządzanych wpisów Calendar i zapisanych decyzji wyłącznie przez odczyt.
   Następnie uruchomić planer tylko na tym snapshotcie, bez narzędzi i zapisów, i porównać
   wynik z oczekiwanymi czynnościami. Na tym etapie zero zapisów Calendar i stanu aktywnego.
   Runtime pozostaje jawnie skonfigurowany jako `gpt-6.1-sol/high`, bez automatycznej zmiany modelu.
   Bez ASTRA/Ultra, dodatkowych modeli kontrolnych i pętli „próbujemy aż wyjdzie”.
2. **Implementacja po akceptacji podejścia:** delegowany worker na najnowszym dostępnym
   SOL/high, jawny zakres plików i walidacja; koordynator integruje i przegląda.
   Xhigh dla workera tylko przy wykazanej, ograniczonej trudności.
   Nieudany przypadek daje konkretny błędny input/output i wąską przyczynę przed poprawką.
3. **Kilka testów akceptacyjnych:** wspólne scenariusze z kontrolowanym zegarem i fake
   Calendar; publiczne dane wyłącznie fikcyjne. Oczekiwane wyniki:
   - Ta sama czynność w trzech źródłach → jedna agenda, trzy powiązania, jeden wpis;
     osobny zwykły obowiązek szkolny → automat, oferta pozaszkolna → review.
   - Poprawna, lecz niepewna czynność obok niezależnego jasnego obowiązku → tylko pierwsza
     trafia do review; druga może zostać wykonana.
   - Na prywatnych danych: przypomnienie o materiałach szkolnych trafia dokładnie na dzień
     wynikający ze źródła, bez wymyślonego dodatkowego zadania domowego.
   - Na prywatnych danych: uroczystość szkolna łączy się z istniejącym ID wydarzenia
     i zachowuje ręcznie zatwierdzone godziny. Treści, daty i ID pozostają poza repozytorium.
   - Nowa wersja źródła zachowuje zatwierdzone godziny i odrzucenie; oferta pięciu dat
     po wyborze tworzy dokładnie jeden wpis i nie otwiera ponownie pozostałych.
   - Migracja zachowuje wszystkie dotychczasowe ID; replay i niezmieniona synchronizacja
     dają zero dodatkowych zapisów i zero inference, lecz nadal rozliczają decyzje/unknown.
   - Timeout po wykonanym create → odczyt rozpoznaje istniejący wpis, bez drugiego create;
     ręczna zmiana Calendar → review przed nadpisaniem; zmiana decyzji unieważnia plan.
   - Niedostępne źródło i pominięta czynność → zero usunięć; przepełniony kontekst lub
     błędny plan → widoczny błąd, zero zapisów; preview → zero mutacji produkcyjnych.
4. **Przełączenie w ramach późniejszego zleconego wykonania po testach i POC na aktualnych danych:** publiczny przejrzany
   PR i zielone CI, następnie kanoniczne `prepare` → `activate` → `verify`.
   Konfigurację obsługiwać zgodnie z zainstalowaną polityką sekretów; identyfikatory
   wdrożenia i wyniki na prawdziwych danych pozostają prywatne.
   Zatrzymać timer aplikacji tylko na przełączenie, pozwolić bieżącemu writerowi skończyć
   i rozliczyć unknown przed migracją. Migracja robi wyłącznie lokalne powiązania,
   zero zapisów Calendar. Włączyć jeden nowy planer, a stary usunąć z aktywnej ścieżki.
   Następnie kontrolowana zwykła synchronizacja może dodać poprawne przyszłe obowiązki;
   sprawdzić zachowanie wszystkich wcześniejszych wpisów i brak duplikatów.
   Przywrócić timer i sprawdzić następne rzeczywiste uruchomienie godzinowe oraz health.
   Błąd zatrzymuje nowe zapisy i wymaga poprawki w przód; bez cofania bazy i dziennika.

Gotowe oznacza: zachowane dotychczasowe wpisy i decyzje, poprawne nowe obowiązki,
brak duplikatów po replay i restarcie, zero unknown oraz jeden aktywny planer.
Po spełnieniu tych warunków zlecone wykonanie zastępuje stary silnik bez dodatkowej bramki zgody.

## Uczenie z przyszłych decyzji rodzica

Opcjonalne uczenie włącza administrator poleceniem `preferences-enable --receipt-file` z prywatnym potwierdzeniem granicy czasowej i listą wykluczonych starych propozycji/ofert. Starsze decyzje nadal chronią konkretne działania, ale nie są przykładami preferencji. Nowy przykład powstaje dopiero po ręcznej akceptacji/odrzuceniu nowej propozycji po tej granicy; wynik automatycznego zapisu i porządkowanie starej kolejki nie uczą systemu.

Ten sam SOL/high dostaje jawne okno ostatnich 50 przykładów oraz liczbę pominiętych starszych. Pełny rejestr zostaje w bazie. Opisy przykładów mają oznaczony skrót do 400 znaków; nie wolno opierać pewnej oceny podobieństwa na pominiętych szczegółach. Co najmniej dwie niezależne zgodne decyzje mogą uzasadnić automatyczną akceptację albo odrzucenie podobnej pojedynczej aktywności. Podobieństwo ocenia model w tym samym zadaniu; kod sprawdza cytowane przykłady i ich niezależność. Sprzeczne lub niewystarczające dane wymagają review.

Wybór jednego z kilku terminów pozostaje ręczny. Niewybrane daty jednej oferty nie są odrębnymi negatywnymi preferencjami. Zostają ograniczenia dat, odbiorcy, ręcznych zmian, odwołań i nieznanych wyników zapisów. Automatyczna akceptacja ma zapisane pochodzenie, ale nie dostaje flagi ręcznego `user_approved`; kolejka i wykonawca ponownie sprawdzają powiązanie z planem oraz niezmienionymi przykładami. Szczegółowy kontrakt i komendy są w [instrukcji uruchomienia](setup.md#future-human-choice-examples).
