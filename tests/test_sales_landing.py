import re
import tempfile
import unittest
from pathlib import Path

from controlplane.plan_policy import plan_defaults
from controlplane.server import SALES_ASSET_FILENAMES, create_controlplane_app
from controlplane.settings import ControlPlaneSettings


class SalesLandingTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        settings = ControlPlaneSettings(
            database_path=root / "control.sqlite",
            tenant_data_root=root / "tenants",
            tenant_backup_root=root / "backups",
            tenant_base_domain="shops.example.test",
            bot_token="123456:sales-test-token",
            admin_telegram_ids=frozenset({101}),
            host="127.0.0.1",
            port=8100,
        )
        self.client = create_controlplane_app(settings).test_client()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_sales_is_public_static_page_with_restrictive_headers(self):
        response = self.client.get("/sales")
        try:
            self.assertEqual(200, response.status_code)
            self.assertEqual("text/html", response.mimetype)
            self.assertEqual("ru", response.headers["Content-Language"])
            self.assertEqual("public, max-age=300", response.headers["Cache-Control"])
            self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
            self.assertEqual("DENY", response.headers["X-Frame-Options"])
            self.assertEqual("no-referrer", response.headers["Referrer-Policy"])
            self.assertIn("payment=()", response.headers["Permissions-Policy"])
            csp = response.headers["Content-Security-Policy"]
            for directive in (
                "default-src 'none'",
                "connect-src 'none'",
                "object-src 'none'",
                "base-uri 'none'",
                "form-action 'none'",
                "frame-src 'none'",
                "frame-ancestors 'none'",
            ):
                self.assertIn(directive, csp)
        finally:
            response.close()

    def test_sales_only_allows_get(self):
        self.assertEqual(405, self.client.post("/sales").status_code)

    def test_sales_assets_are_allowlisted_same_origin_webp(self):
        self.assertEqual(
            {
                "bot-analytics.webp",
                "bot-branding.webp",
                "bot-broadcast.webp",
                "bot-categories.webp",
                "bot-customer-texts.webp",
                "bot-delivery-settings.webp",
                "bot-orders.webp",
                "bot-payment-settings.webp",
                "bot-promo-codes.webp",
                "sales-admin-dashboard.webp",
                "sales-buyer-cart.webp",
                "sales-buyer-orders.webp",
                "sales-catalog-import.webp",
                "sales-catalog-management.webp",
                "sales-miniapp-branding.webp",
                "sales-miniapp-checkout-settings.webp",
                "sales-miniapp-database-backups.webp",
                "sales-miniapp-message-templates.webp",
                "sales-miniapp-promo-codes.webp",
                "sales-miniapp-staff-roles.webp",
                "sales-operations-journal.webp",
                "sales-operations-overview.webp",
                "sales-referral-analytics.webp",
                "sales-sales-analytics.webp",
                "sales-staff.webp",
                "sales-storefront-compact.webp",
                "sales-storefront-dark.webp",
                "sales-storefront-light.webp",
                "sales-warehouse-assembly.webp",
            },
            SALES_ASSET_FILENAMES,
        )
        for asset_name in SALES_ASSET_FILENAMES:
            response = self.client.get(f"/sales/assets/{asset_name}")
            try:
                self.assertEqual(200, response.status_code)
                self.assertEqual("image/webp", response.mimetype)
                self.assertEqual("public, max-age=300", response.headers["Cache-Control"])
                self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
                self.assertEqual("no-referrer", response.headers["Referrer-Policy"])
            finally:
                response.close()
        self.assertEqual(404, self.client.get("/sales/assets/not-published.webp").status_code)
        self.assertEqual(404, self.client.get("/sales/assets/../sales_landing.html").status_code)
        self.assertEqual(405, self.client.post("/sales/assets/bot-orders.webp").status_code)

    def test_sales_contract_has_demo_pricing_and_no_production_integrations(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()

        for expected in (
            "Универсальная платформа продаж",
            "товары и услуги",
            "пользователи",
            "@Bubblecubby",
            "Данные не сохраняются",
            "Витрина",
            "Операции",
            "Рост",
            "Настройка",
            "Администратор",
            "Видит пользователь",
            "Видит администратор",
            "Гибкая настройка витрины",
            'id="scenarios"',
            'data-scenario-card="goods"',
            'id="scenario-choice-note"',
            'Сначала выберите, что вы продаёте.',
            'Покупатель:',
            'Команда:',
            'ШАГ 2 · ИНТЕРАКТИВНАЯ ДЕМОНСТРАЦИЯ',
            'Пройдите путь заказа по шагам.',
            '#Ваша_платформа',
            'DEFAULT_STORE_NAME',
            'applyScenarioChoice()',
            'setupSectionReveals',
            'data-reveal',
            'motion-ready',
            'IntersectionObserver',
            'demo-update',
            'data-scenario="goods"',
            'data-scenario="services"',
            'data-scenario="digital"',
            'id="journey-steps"',
            'data-journey-step="issuance"',
            'data-journey-step="receipt"',
            'id="story-buyer"',
            'id="story-admin"',
            'id="story-warehouse"',
            'id="story-issuance"',
            'id="story-receipt"',
            'id="connector-to-admin"',
            'id="connector-to-warehouse"',
            'id="connector-to-issuance"',
            'id="connector-to-receipt"',
            'id="handoff-to-admin"',
            'id="handoff-to-warehouse"',
            'id="handoff-to-issuance"',
            'id="handoff-to-receipt"',
            'Проверить и подтвердить оплату',
            'готов к выдаче',
            'Подготовить к выдаче и открыть итог',
            'Локальная DEMO в браузере',
            'story-arrow',
            'story-enter',
            'story-update',
            'scenario-choice',
            'scenario-handoff',
            'renderStoryStages',
            'renderIssuance',
            'renderReceipt',
            'applyScenarioPreview',
            'prepareIssuance',
            'completeIssuance',
            'animateScenarioChoice',
            'white-space: nowrap',
            '.price-card.recommended { padding-top: 52px;',
            'id="plan-comparison"',
            'data-plan-table',
            'id="launch"',
            'id="faq"',
            '<details class="faq-item">',
            'id="storefront-title"',
            'id="admin-store-title"',
            'id="user-order-view"',
            'id="admin-order-view"',
            'id="warehouse-order-view"',
            "simulate-demo-payment",
            "confirm-demo-order",
            "claim-demo-order",
            'DEMO-',
            'const createOrder = () =>',
            'const submitPayment = () =>',
            'const confirmOrder = () =>',
            'const claimOrder = () =>',
            'const packOrder = () =>',
            'const calculateCartTotals = () =>',
            'Собран',
            'не являются публичной офертой',
            'Недоступно</td><td>5</td><td>20',
            '0</td><td>2</td><td>2',
            '0</td><td>0</td><td>20',
            '0</td><td>0</td><td>2 000',
            'id="storefront-title"',
            'id="admin-store-title"',
            'id="catalog-density-note"',
            'id="story-admin"',
            'id="demo-settings"',
            'id="growth-preview"',
            'id="settings-preview"',
            'Формат DEMO-каталога',
            'id="buyer-interface-evidence"',
            'id="warehouse-assembly-evidence"',
            'id="story-issuance"',
            'id="miniapp-screenshots"',
            'href="#miniapp-screenshots" data-landing-target="miniapp-screenshots">Галерея</a>',
            'id="landing-tab-list"',
            'role="tablist"',
            'id="overview-tab"',
            'id="demo-tab"',
            'id="gallery-tab"',
            'id="overview-panel"',
            'id="demo-panel"',
            'id="gallery-panel"',
            'data-landing-tab="overview"',
            'data-landing-tab="demo"',
            'data-landing-tab="gallery"',
            'activateLandingTabForHash',
            'ArrowRight',
            'ArrowLeft',
            'ЭКРАНЫ РАБОЧЕГО ПРОСТРАНСТВА',
            'необратимо обезличены',
            'id="buyer-screens"',
            'id="operations-screens"',
            'id="growth-screens"',
            'id="launch-screens"',
            '/sales/assets/bot-categories.webp',
            '/sales/assets/bot-orders.webp',
            '/sales/assets/bot-analytics.webp',
            '/sales/assets/bot-branding.webp',
            '/sales/assets/sales-buyer-cart.webp',
            '/sales/assets/sales-warehouse-assembly.webp',
            '/sales/assets/sales-admin-dashboard.webp',
            'loading="lazy"',
            'data-open-storefront',
            'data-open-admin-demo="inventory"',
            'data-open-admin-demo="orders"',
            'data-open-admin-demo="growth"',
            "const navigateToDemoTarget =",
            "window.requestAnimationFrame(() => {",
            "focus({ preventScroll: true })",
            "id=\"admin-tools-result\"",
            "id=\"open-admin-inventory\"",
            "id=\"open-admin-orders\"",
            'id="showcase-row"',
            'data-showcase="services"',
            'id="density-row"',
            'id="open-demo-storefront"',
            "Открыта интерактивная витрина. Данные не отправляются.",
            "navigateToDemoTarget('story-buyer'",
            "prefers-reduced-motion: reduce",
            ".price-card:hover, .price-card:focus-within",
            ".price-card::before",
            "applyScenarioPreview()",
            "data-scenario-motion=\"goods\"",
            "data-scenario-motion=\"services\"",
            "data-scenario-motion=\"digital\"",
            "@keyframes scenario-goods",
            "@keyframes scenario-services",
            "@keyframes scenario-digital",
            "applyDensity()",
            "byId('product-list').dataset.density = state.density",
            "Компактный вид карточек: больше позиций в экране.",
            "byId('storefront-title').textContent",
            "byId('admin-store-title').textContent",
            "не являются публичной офертой",
            "согласуются индивидуально в Telegram",
            "5 900 ₽",
            "159 000 ₽",
            "prefers-reduced-motion",
            'id="reset-demo"',
            "DEMO10",
            "https://t.me/Bubblecubby",
            'rel="noopener noreferrer"',
        ):
            self.assertIn(expected.casefold(), source.casefold())
        for forbidden in (
            "Семена Знаний",
            "книжный магазин",
            "книги",
            "читатели",
            "fetch(",
            "XMLHttpRequest",
            "WebSocket",
            "EventSource",
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "document.cookie",
            "telegram-web-app.js",
            "Telegram.WebApp",
            "/api/",
            "<form",
            "BOT_TOKEN",
            "initData",
        ):
            self.assertNotIn(forbidden.casefold(), source.casefold())

    def test_scenario_picker_separates_customer_and_team_paths(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        scenarios_start = source.index('id="scenarios"')
        demo_start = source.index('id="demo"')
        picker_end = source.index("</section>", scenarios_start)
        self.assertLess(scenarios_start, demo_start)
        self.assertLess(picker_end, demo_start)
        self.assertLess(source.index('id="demo-settings"'), source.index('id="story-buyer"'))
        self.assertNotIn('id="utility-tabs"', source)
        self.assertNotIn('const setUtilityTab', source)
        picker = source[scenarios_start:picker_end]
        self.assertEqual(3, picker.count('data-scenario-card='))
        self.assertEqual(3, picker.count('data-scenario="'))
        for marker in (
            '<strong>Покупатель:</strong>',
            '<strong>Команда:</strong>',
            'id="scenario-choice-note"',
            'aria-live="polite"',
            'aria-pressed="true"',
            'ШАГ 2 · ИНТЕРАКТИВНАЯ ДЕМОНСТРАЦИЯ',
            'const setupSectionReveals = () =>',
            "prefers-reduced-motion: reduce",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("Семена Знаний", source)

    def test_format_controls_share_scenario_state_without_product_editor(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        scenario_start = source.index('id="scenarios"')
        scenario_end = source.index('id="demo"', scenario_start)
        showcase_start = source.index('id="showcase-row"')
        showcase_end = source.index('id="density-row"', showcase_start)
        scenarios = source[scenario_start:scenario_end]
        showcase = source[showcase_start:showcase_end]
        for value in ("goods", "services", "digital"):
            self.assertIn(f'data-scenario="{value}"', scenarios)
            self.assertIn(f'data-showcase="{value}"', showcase)
            self.assertIn(f'data-scenario-motion="{value}"', source)
        self.assertIn("if (button) selectScenario(button.dataset.showcase);", source)
        self.assertIn("const scenario = scenarios[state.scenario];", source)
        self.assertIn("requiresPacking: lines.some(line => line.stockTracked)", source)
        self.assertIn("order && !order.requiresPacking", source)
        self.assertNotIn("state.showcase", source)
        self.assertNotIn("applyShowcase", source)
        self.assertNotIn("только в preview витрины; текущий DEMO-заказ не затронут", source)
        for marker in (
            "product-edit-demo",
            "product-edit-title",
            "product-edit-price",
            "product-edit-availability",
            "product-edit-delivery",
            "product-edit-result",
            "initialProductDraft",
            "productDraft",
            "renderProductEdit",
            "applyProductEdit",
            "apply-product-edit",
            ".product-edit",
        ):
            self.assertNotIn(marker, source)

    def test_vertical_demo_story_keeps_actions_and_handoffs_in_order(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        markers = (
            'id="story-buyer"',
            'id="connector-to-admin"',
            'id="story-admin"',
            'id="connector-to-warehouse"',
            'id="story-warehouse"',
            'id="connector-to-issuance"',
            'id="story-issuance"',
            'id="connector-to-receipt"',
            'id="story-receipt"',
        )
        positions = [source.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        for marker in (
            "paymentButton.id = 'simulate-demo-payment'",
            "confirmButton.id = 'confirm-demo-order'",
            "claimButton.id = 'claim-demo-order'",
            'id="pack-order"',
            'id="handoff-to-admin"',
            'id="handoff-to-warehouse"',
            'id="handoff-to-issuance"',
            'id="handoff-to-receipt"',
            'id="prepare-issuance"',
            'Проверить и подтвердить оплату',
            'локальная DEMO',
            'const renderStoryStages = () =>',
            'const renderIssuance = () =>',
            'const renderReceipt = () =>',
            'const prepareIssuance = () =>',
            'const completeIssuance = () =>',
            'const animateStory =',
            "prefers-reduced-motion: no-preference",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("(() => { return;", source)

    def test_guided_demo_navigation_has_one_issuance_action_and_admin_fallbacks(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        for marker in (
            "const navigateToDemoTarget =",
            "activateLandingTab(landingTabForTarget(target));",
            "window.requestAnimationFrame(() => {",
            "target.scrollIntoView({ behavior: isReducedMotion() ? 'auto' : 'smooth', block: 'start' })",
            "focus({ preventScroll: true })",
            "navigateToDemoTarget('story-admin', '#story-admin-heading', 'connector-to-admin', 'story-admin')",
            "navigateToDemoTarget(order.requiresPacking ? 'story-warehouse' : 'story-receipt'",
            "navigateToDemoTarget('story-issuance', '#story-issuance-heading'",
            "navigateToDemoTarget('story-receipt', '#story-receipt-heading'",
            "id=\"admin-tools-result\"",
            "id=\"admin-inventory-note\"",
            "id=\"open-admin-inventory\"",
            "id=\"open-admin-orders\"",
            "inventory.disabled = noInventory;",
            "if (!order || order.paymentStatus !== 'confirmed')",
            "navigateToDemoTarget('story-admin', '#confirm-demo-order', 'story-admin')",
            "В этом формате складские остатки не используются.",
            "Передача отмечена в DEMO. Ниже открыт итог для покупателя.",
        ):
            self.assertIn(marker, source)
        self.assertNotIn('id="complete-issuance"', source)
        self.assertNotIn("byId('complete-issuance')", source)
        self.assertNotIn("scrollToStory", source)

    def test_landing_navigation_reuses_guided_transition_and_focused_headings(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        overview_targets = {
            "features": "features-heading",
            "pricing": "pricing-heading",
            "launch": "launch-heading",
        }
        for target_id, heading_id in overview_targets.items():
            self.assertIn(f'href="#{target_id}" data-overview-target="{target_id}"', source)
            self.assertIn(f'id="{target_id}" class="section" aria-labelledby="{heading_id}"', source)
            self.assertIn(f'id="{heading_id}" tabindex="-1"', source)
            self.assertIn(f"{target_id}: '#{heading_id}'", source)
        for marker in (
            'href="#miniapp-screenshots" data-landing-target="miniapp-screenshots"',
            'id="gallery-tab" class="landing-tab"',
            'aria-controls="gallery-panel"',
            'id="gallery-panel" class="landing-panel" role="tabpanel" aria-labelledby="gallery-tab" hidden',
            "gallery: { tab: byId('gallery-tab'), panel: byId('gallery-panel') }",
            "landingTabs.gallery.panel.append(byId('miniapp-screenshots'));",
            "const landingTabForTarget = target =>",
            "const landingNavigation = Object.freeze({",
            "'miniapp-screenshots': '#miniapp-screenshots-heading'",
            "const navigateToLandingTarget = (targetId, updateHistory = false) =>",
            "navigateToDemoTarget(targetId, focusSelector)",
            "history.pushState(null, '', hash)",
            "lastHandledLandingHash",
            "link.dataset.landingTarget === targetId",
            "window.addEventListener('popstate', activateLandingTabForHash)",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("const overviewNavigation", source)
        self.assertNotIn("scrollToOverview", source)

    def test_overview_reveals_replay_smoothly_in_both_directions(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        self.assertIn(
            "if (key !== 'overview') selected.panel.querySelectorAll('[data-reveal]').forEach(element => element.classList.add('is-visible'));",
            source,
        )
        for marker in (
            '<section id="features" class="section" aria-labelledby="features-heading"><div class="page" data-reveal>',
            'id="buyer-screens" class="screenshot-group" data-reveal',
            'id="operations-screens" class="screenshot-group" data-reveal',
            'id="growth-screens" class="screenshot-group" data-reveal',
            'id="launch-screens" class="screenshot-group" data-reveal',
            '<section id="pricing" class="section" aria-labelledby="pricing-heading"><div class="page" data-reveal>',
            '<section id="launch" class="section" aria-labelledby="launch-heading"><div class="page" data-reveal>',
            "window.__salesInitialScroll",
            "history.scrollRestoration = 'manual'",
            "const settleInitialScroll = (initial, pageShown = document.readyState === 'complete') =>",
            "if (event.persisted) settleInitialScroll(prepareInitialScroll(), true);",
            "let armOverviewReveals = () => {};",
            "if (key === 'overview') armOverviewReveals();",
            ".motion-ready #overview-panel [data-reveal].reveal-armed",
            ".reveal-armed.reveal-from-below",
            ".reveal-armed.reveal-from-above",
            "opacity .84s cubic-bezier(.16,1,.3,1)",
            "const offscreenDirection = rect =>",
            "if (rect.bottom <= 0) return 'above';",
            "if (rect.top >= window.innerHeight) return 'below';",
            "const makeVisible = section =>",
            "const arm = (section, direction) =>",
            "const syncOverviewReveals = () =>",
            "sections.forEach(section => observer.observe(section));",
            "armOverviewReveals = syncOverviewReveals;",
            "threshold: 0, rootMargin: '0px'",
            "prefers-reduced-motion: reduce",
            "!('IntersectionObserver' in window)",
        ):
            self.assertIn(marker, source)
        for removed in (
            "reveal-pending",
            "const pending = new Set();",
            "observer.unobserve(section);",
            "observer.disconnect();",
            "entry.boundingClientRect.top < 0",
        ):
            self.assertNotIn(removed, source)
        self.assertNotIn("@keyframes overview-reveal", source)
        self.assertNotIn(".motion-ready [data-reveal] { opacity: 0", source)
        self.assertNotIn("navigation?.type !== 'navigate'", source)

    def test_screenshot_gallery_uses_only_reviewed_local_assets(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        image_sources = re.findall(r'<img[^>]+src="([^"]+)"', source)
        expected_sources = {f"/sales/assets/{name}" for name in SALES_ASSET_FILENAMES}
        self.assertEqual(expected_sources, set(image_sources))
        self.assertEqual(len(SALES_ASSET_FILENAMES), len(image_sources))
        for image_source in expected_sources:
            self.assertEqual(1, image_sources.count(image_source))
        self.assertNotIn("bot-dialogs.webp", source)
        self.assertIn("необратимо обезличены", source.casefold())
        self.assertIn('.screenshot-gallery { columns: 3;', source)
        self.assertIn('.screenshot-gallery { columns: 2;', source)
        self.assertIn('.screenshot-gallery { columns: 1;', source)
        self.assertIn('break-inside: avoid', source)
        self.assertNotIn('grid-auto-flow: dense', source)
        self.assertLess(source.index('id="story-buyer"'), source.index('id="buyer-interface-evidence"'))
        self.assertLess(source.index('id="buyer-interface-evidence"'), source.index('id="connector-to-admin"'))
        operations_start = source.index('id="operations-screens"')
        operations_end = source.index('id="growth-screens"')
        operations = source[operations_start:operations_end]
        admin_start = source.index('id="story-admin"')
        admin_end = source.index('id="connector-to-warehouse"')
        admin = source[admin_start:admin_end]
        for asset in ('bot-categories.webp', 'sales-catalog-management.webp'):
            self.assertIn(asset, operations)
            self.assertNotIn(asset, admin)
        self.assertLess(source.index('id="story-warehouse"'), source.index('id="warehouse-assembly-evidence"'))
        self.assertLess(source.index('id="warehouse-assembly-evidence"'), source.index('id="connector-to-issuance"'))
        self.assertLess(source.index('id="story-receipt"'), source.index('id="miniapp-screenshots"'))
        self.assertLess(source.index('id="miniapp-screenshots"'), source.index('id="pricing"'))
        gallery_groups = (
            'id="buyer-screens"',
            'id="operations-screens"',
            'id="growth-screens"',
            'id="launch-screens"',
        )
        group_positions = [source.index(group) for group in gallery_groups]
        self.assertEqual(group_positions, sorted(group_positions))
        self.assertEqual(len(SALES_ASSET_FILENAMES), source.count('loading="lazy"'))

    def test_gallery_lightbox_stays_on_page_and_covers_every_gallery_image(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        gallery_start = source.index('id="miniapp-screenshots"')
        gallery_end = source.index('id="pricing"', gallery_start)
        gallery = source[gallery_start:gallery_end]
        self.assertEqual(1, source.count('<dialog id="image-lightbox"'))
        self.assertEqual(1, source.count('id="image-lightbox-image"'))
        self.assertNotIn('id="image-lightbox-image" class="image-lightbox-image" src=', source)
        self.assertEqual(len(SALES_ASSET_FILENAMES) - 4, gallery.count('data-image-lightbox-trigger'))
        self.assertEqual(len(SALES_ASSET_FILENAMES) - 4, gallery.count('aria-haspopup="dialog"'))
        self.assertNotIn('<a ', gallery)
        for marker in (
            'const imageLightbox = byId(\'image-lightbox\');',
            'imageLightbox.showModal();',
            "imageLightboxClose.addEventListener('click', closeImageLightbox);",
            "imageLightbox.addEventListener('close', () => {",
            "trigger.focus({ preventScroll: true });",
            "if (event.target === imageLightbox) closeImageLightbox();",
            "imageLightboxImage.removeAttribute('src');",
            '.screenshot-image-trigger {',
            'cursor: zoom-in',
        ):
            self.assertIn(marker, source)

    def test_mini_app_gallery_copy_states_current_boundaries(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        for marker in (
            'sales-miniapp-staff-roles.webp',
            'sales-miniapp-promo-codes.webp',
            'sales-miniapp-checkout-settings.webp',
            'sales-miniapp-message-templates.webp',
            'sales-miniapp-branding.webp',
            'sales-miniapp-database-backups.webp',
            'Администратор магазина — отдельная роль, не владелец; назначать её может только владелец.',
            'секреты платёжных сервисов на этом экране не показываются',
            'обязательные подстановки',
            'Telegram HTML-разметку',
            'Только владелец создаёт и скачивает проверенные копии базы',
            'обложки, страницы книг и другие медиа не входят',
            'автоматически базу не восстанавливает',
        ):
            self.assertIn(marker, source)

    def test_scenario_cards_share_tariff_card_motion_and_confirm_selection(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        for marker in (
            '.scenario-card, .price-card {',
            '.scenario-card:hover, .scenario-card:focus-within, .price-card:hover, .price-card:focus-within',
            '.scenario-card.is-selected.is-choice-updated',
            "card.classList.add('is-choice-updated');",
            'let scenarioNavigationTimer = 0;',
            'window.clearTimeout(scenarioNavigationTimer);',
            'scenarioNavigationTimer = window.setTimeout',
            'if (!animateScenarioChoice()) {',
        ):
            self.assertIn(marker, source)

    def test_demo_uses_an_accessible_panel_and_advances_issuance_to_receipt(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        for marker in (
            'role="tab"',
            'role="tabpanel"',
            'aria-controls="overview-panel"',
            'aria-controls="demo-panel"',
            'aria-controls="gallery-panel"',
            'aria-labelledby="overview-tab"',
            'aria-labelledby="demo-tab"',
            'aria-labelledby="gallery-tab"',
            'id="demo-panel" class="landing-panel" role="tabpanel" aria-labelledby="demo-tab" hidden',
            'id="gallery-panel" class="landing-panel" role="tabpanel" aria-labelledby="gallery-tab" hidden',
            "landingTabs.demo.panel.append(byId('scenarios'), byId('demo'))",
            "landingTabs.gallery.panel.append(byId('miniapp-screenshots'));",
            "if (key !== 'overview') selected.panel.querySelectorAll('[data-reveal]').forEach(element => element.classList.add('is-visible'));",
            "window.addEventListener('hashchange', activateLandingTabForHash)",
            "order.issuance = 'ready';",
            "completeIssuance();",
        ):
            self.assertIn(marker, source)
        prepare_start = source.index("const prepareIssuance = () =>")
        prepare_end = source.index("function applyAccent", prepare_start)
        prepare_body = source[prepare_start:prepare_end]
        self.assertLess(prepare_body.index("order.issuance = 'ready';"), prepare_body.index("completeIssuance();"))
        self.assertNotIn("resetDemo()", prepare_body)
        self.assertEqual(1, source.count('href="#miniapp-screenshots"'))

    def test_gallery_tab_mobile_layout_and_birthday_surprise_are_local(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        for marker in (
            'id="gallery-tab" class="landing-tab" type="button" role="tab" aria-controls="gallery-panel"',
            'id="gallery-panel" class="landing-panel" role="tabpanel" aria-labelledby="gallery-tab" hidden',
            "landingTabs.gallery.panel.append(byId('miniapp-screenshots'));",
            "const landingTabForTarget = target =>",
            "if (key !== 'overview') selected.panel.querySelectorAll('[data-reveal]').forEach(element => element.classList.add('is-visible'));",
            '.landing-tab-list { display: grid; width: 100%; grid-template-columns: repeat(3, minmax(0, 1fr)); }',
            'overflow-wrap: anywhere',
            '.phone { width: min(100%, 300px); }',
            '.brand-cluster { flex: 1 1 auto;',
            '.wordmark-name { overflow: hidden; text-overflow: ellipsis; }',
            'id="birthday-trigger" class="brand-surprise" type="button"',
            '<dialog id="birthday-surprise" class="birthday-surprise"',
            'С днем рождения Лера &lt;3',
            'class="birthday-number" aria-hidden="true">21',
            '<span class="birthday-token">🎈</span>',
            'const birthdayTriggerWindowMs = 2500;',
            'const birthdayTriggerCount = 7;',
            'birthdayTriggerPresses = birthdayTriggerPresses.filter',
            'birthdaySurprise.showModal();',
            "birthdaySurprise.addEventListener('close', () => {",
            'birthdayTrigger.focus({ preventScroll: true });',
            '@media (prefers-reduced-motion: no-preference) {',
            '.birthday-token { animation: birthday-float',
            'max-height: 94svh;',
            'height: 100svh;',
            'height: 100dvh;',
        ):
            self.assertIn(marker, source)
        self.assertNotIn('max-height: 94vh;', source)
        self.assertNotIn('max-height: min(72vh, 880px);', source)
        self.assertNotIn('height: 100vh;', source)

    def test_plan_comparison_matches_standard_entitlements(self):
        response = self.client.get("/sales")
        try:
            source = response.get_data(as_text=True)
        finally:
            response.close()
        defaults = plan_defaults()
        values = {
            "positions": [
                defaults[plan]["limits"]["books"]
                for plan in ("start", "business", "pro")
            ],
            "staff": [
                defaults[plan]["limits"]["staff_members"]
                for plan in ("business", "pro")
            ],
            "imports": [
                defaults[plan]["limits"]["imports_per_day"]
                for plan in ("start", "business", "pro")
            ],
            "campaigns": [
                defaults[plan]["limits"]["campaigns"]
                for plan in ("start", "business", "pro")
            ],
            "broadcasts": [
                defaults[plan]["limits"]["broadcast_recipients_per_day"]
                for plan in ("start", "business", "pro")
            ],
        }
        formatted = {
            name: [f"{value:,}".replace(",", " ") for value in limits]
            for name, limits in values.items()
        }
        for name, limits in formatted.items():
            self.assertIn(f'data-plan-limit="{name}"', source)
            for value in limits:
                self.assertIn(f">{value}</td>", source)
        self.assertIn(
            "Активные сотрудники</th><td class=\"unavailable\">Недоступно</td>",
            source,
        )
        self.assertIn("Кампании и рассылки", source)

    def test_root_stays_console_and_platform_api_stays_protected(self):
        root = self.client.get("/")
        try:
            self.assertEqual(200, root.status_code)
            self.assertIn("White-label platform", root.get_data(as_text=True))
        finally:
            root.close()
        self.assertEqual(401, self.client.get("/api/platform/session").status_code)

    def test_sales_file_is_allowlisted_for_deterministic_release(self):
        repository = Path(__file__).resolve().parents[1]
        allowlist = (repository / "deploy" / "release-allowlist.txt").read_text(encoding="utf-8")
        allowlisted_paths = allowlist.splitlines()
        self.assertIn("controlplane/sales_landing.html", allowlisted_paths)
        for asset_name in SALES_ASSET_FILENAMES:
            self.assertIn(f"controlplane/sales_assets/{asset_name}", allowlisted_paths)


if __name__ == "__main__":
    unittest.main()
