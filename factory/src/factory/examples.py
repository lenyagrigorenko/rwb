"""Compact few-shot example of the IR format from a different domain (equipment maintenance)."""

DOMAIN_EXAMPLE = {
    "slug": "equipment-service", "namespace": "EquipmentService", "product_name": "Сервис обслуживания оборудования",
    "map_center": [55.7558, 37.6173],
    "entities": [
        {"name": "Site", "title": "Объект", "title_plural": "Объекты", "description": "Площадка с оборудованием",
         "display_field": "name", "seed_count": 0, "covers": ["FR-001"],
         "fields": [
             {"name": "code", "title": "Код объекта", "type": "string", "required": True, "unique": True, "max_length": 16, "regex": "^[A-Z]{2}-\\d{3}$", "examples": ["MS-101", "MS-102"]},
             {"name": "name", "title": "Название", "type": "string", "required": True, "max_length": 200, "searchable": True},
             {"name": "location", "title": "Расположение", "type": "geo_point", "required": True, "list_visible": False}]},
        {"name": "ServiceRequest", "title": "Заявка", "title_plural": "Заявки", "description": "Заявка на ремонт",
         "display_field": "number", "owner_field": "technicianUserId", "seed_count": 4, "covers": ["FR-002", "FR-003"],
         "fields": [
             {"name": "number", "title": "Номер", "type": "string", "required": True, "unique": True, "max_length": 12, "readonly": True, "examples": ["SR-0001", "SR-0002"]},
             {"name": "siteId", "title": "Объект", "type": "ref", "ref": "Site", "required": True},
             {"name": "technicianUserId", "title": "Техник", "type": "ref", "ref": "User"},
             {"name": "dueAt", "title": "Срок", "type": "datetime", "required": True},
             {"name": "status", "title": "Статус", "type": "enum", "required": True, "enum_values": ["New", "InProgress", "Done"],
              "enum_titles": {"New": "Новая", "InProgress": "В работе", "Done": "Выполнена"}}],
         "lifecycle": {"field": "status", "initial": "New", "transitions": [
             {"name": "complete", "title": "Завершить", "from_states": ["InProgress"], "to": "Done", "roles": ["Technician"], "emits": "RequestCompleted"}]}}],
    "permissions": [
        {"role": "Dispatcher", "entity": "Site", "actions": ["read", "create", "update"]},
        {"role": "Dispatcher", "entity": "ServiceRequest", "actions": ["read", "create", "update", "delete"]},
        {"role": "Technician", "entity": "Site", "actions": ["read"]},
        {"role": "Technician", "entity": "ServiceRequest", "actions": ["read_own", "update_own"]}],
    "datasets": [{"name": "sites", "file": "sites.json", "records_path": "items", "entity": "Site",
                  "mapping": {"code": "siteCode", "name": "title", "location": "wkt"}, "geometry_format": "wkt"}],
    "integrations": [{"name": "SmsGateway", "title": "SMS-шлюз", "description": "Отправка SMS", "real_system": "SMS-провайдер",
                      "methods": [{"name": "Send", "description": "Отправить SMS",
                                   "input": [{"name": "phone", "title": "Телефон", "type": "phone"}],
                                   "output": [{"name": "messageId", "title": "Id", "type": "string"}],
                                   "mock_response": {"messageId": "mock-{phone}"}}]}],
}

BEHAVIOUR_EXAMPLE = {
    "operations": [
        {"id": "OP-001", "name": "StartRequest", "title": "Взять заявку в работу",
         "description": "Техник берёт свою заявку в работу: загрузить заявку через serviceRequestService.GetAccessibleAsync; "
                        "если статус не New — BusinessRuleException; проверить, что техник находится в 200 м от объекта "
                        "(geo по координатам из запроса), иначе ValidationException; статус InProgress; событие RequestStarted.",
         "method": "POST", "path": "/service-requests/{serviceRequestId}/start", "roles": ["Technician"],
         "input": [{"name": "lat", "title": "Широта", "type": "decimal", "required": True, "min": -90, "max": 90},
                   {"name": "lon", "title": "Долгота", "type": "decimal", "required": True, "min": -180, "max": 180}],
         "output_entity": "ServiceRequest", "emits": ["RequestStarted"], "idempotent": True, "covers": ["FR-003"]}],
    "events": [
        {"name": "RequestStarted", "title": "Заявка взята в работу", "topic": "service-request-events", "key_field": "requestId",
         "recipient_field": "technicianUserId", "notify": "Заявка {number} взята в работу",
         "payload": [{"name": "requestId", "title": "Заявка", "type": "string"}, {"name": "number", "title": "Номер", "type": "string"},
                     {"name": "technicianUserId", "title": "Техник", "type": "string"}]},
        {"name": "RequestOverdue", "title": "Заявка просрочена", "topic": "service-request-events", "key_field": "requestId",
         "recipient_field": "technicianUserId", "notify": "Срок заявки {number} истёк",
         "payload": [{"name": "requestId", "title": "Заявка", "type": "string"}, {"name": "number", "title": "Номер", "type": "string"},
                     {"name": "technicianUserId", "title": "Техник", "type": "string"}]},
        {"name": "RequestCompleted", "title": "Заявка выполнена", "topic": "service-request-events", "key_field": "id",
         "payload": [{"name": "id", "title": "Заявка", "type": "string"}]}],
    "jobs": [{"name": "CheckOverdueRequests", "title": "Контроль сроков", "interval_seconds": 30,
              "description": "Заявки New/InProgress с dueAt в прошлом и без события RequestOverdue: опубликовать RequestOverdue.",
              "emits": ["RequestOverdue"], "covers": ["FR-004"]}],
    "screens": [
        {"id": "SCR-001", "name": "Overview", "title": "Обзор заявок", "platform": "web", "roles": ["Dispatcher"], "covers": ["FR-005"],
         "widgets": [{"type": "kpi", "title": "Новые заявки", "entity": "ServiceRequest", "props": {"filter": {"status": "New"}}},
                     {"type": "map", "title": "Объекты", "props": {"layers": ["Site"]}},
                     {"type": "event_feed", "title": "События"}]},
        {"id": "SCR-002", "name": "MyRequests", "title": "Мои заявки", "platform": "mobile", "roles": ["Technician"], "covers": ["FR-003"],
         "widgets": [{"type": "list", "title": "Заявки", "entity": "ServiceRequest"},
                     {"type": "action_button", "title": "Взять в работу", "operation": "StartRequest"},
                     {"type": "notifications", "title": "Уведомления"}]}],
    "demo": {"title": "Техник выполняет заявку", "steps": [
        {"id": "D1", "title": "Техник берёт заявку в работу", "action": "operation", "operation": "StartRequest", "args": {"lat": 55.75, "lon": 37.61}},
        {"id": "D2", "title": "Заявка в работе", "action": "expect", "args": {"event": "RequestStarted"}},
        {"id": "D3", "title": "Истёк срок", "action": "advance_time", "minutes": 120},
        {"id": "D4", "title": "Уведомление о просрочке", "action": "expect", "args": {"event": "RequestOverdue"}}]},
}
