package app.mobile

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.osmdroid.config.Configuration
import org.osmdroid.tileprovider.tilesource.TileSourceFactory
import org.osmdroid.util.GeoPoint
import org.osmdroid.views.MapView
import org.osmdroid.views.overlay.Marker
import org.osmdroid.views.overlay.Polygon
import org.osmdroid.views.overlay.Polyline
import java.time.Duration
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** Состояние экрана: результаты GET-операций виджетов и уведомления, обновляются каждые 3 секунды. */
class ScreenState {
    val results = mutableStateMapOf<String, JsonElement>()
    val notifications = mutableStateOf<List<JsonObject>>(emptyList())
    val error = mutableStateOf<String?>(null)
}

@Composable
fun ScreenContent(screen: ScreenSpec, modifier: Modifier = Modifier) {
    val state = remember(screen.name) { ScreenState() }
    val context = LocalContext.current
    val seen = remember { mutableSetOf<String>() }
    LaunchedEffect(screen.name) {
        var first = true
        while (true) {
            try {
                screen.widgets.mapNotNull { it.operation }.distinct().forEach { name ->
                    val op = AppSpec.operations[name] ?: return@forEach
                    if (op.method == "GET") state.results[name] = Api.request("GET", Api.resolvePath(op.path))
                }
                screen.widgets.filter { it.type == "list" && it.entity != null }.forEach { w ->
                    val e = AppSpec.entity(w.entity!!) ?: return@forEach
                    state.results["list:${e.name}"] = Api.request("GET", "/api/v1/${e.route}?pageSize=20")
                }
                val items = Api.request("GET", "/api/v1/me/notifications").jsonArray.map { it.jsonObject }
                state.notifications.value = items
                items.forEach { n ->
                    val id = n.str("id")
                    if (seen.add(id) && !first) notify(context, n.str("title"), n.str("body"))
                }
                first = false
                state.error.value = null
            } catch (e: Exception) {
                state.error.value = e.message
            }
            delay(3000)
        }
    }
    LazyColumn(modifier.padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Spacer(Modifier.height(4.dp)) }
        state.error.value?.let { item { Text(it, color = MaterialTheme.colorScheme.error) } }
        items(screen.widgets) { w -> Widget(w, state) }
        item { Spacer(Modifier.height(12.dp)) }
    }
}

@Composable
fun Widget(w: WidgetSpec, state: ScreenState) {
    when (w.type) {
        "map" -> MapWidget(w, state)
        "status_card" -> StatusCard(w, state)
        "countdown" -> Countdown(w, state)
        "action_button" -> ActionButton(w)
        "notifications" -> NotificationsWidget(w, state)
        "list", "table" -> ListWidget(w, state)
        else -> SectionCard(w.title) { Text("Виджет «${w.type}» доступен в web-интерфейсе") }
    }
}

@Composable
fun SectionCard(title: String, content: @Composable () -> Unit) {
    Card(Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            content()
        }
    }
}

@Composable
fun StatusCard(w: WidgetSpec, state: ScreenState) {
    val op = AppSpec.operations[w.operation]
    val data = state.results[w.operation] as? JsonObject
    SectionCard(w.title) {
        if (data == null) Text("Загрузка…") else {
            val fields = op?.outputs?.ifEmpty { null } ?: data.keys.map { FieldSpec(it, it, "string") }
            fields.forEach { f ->
                val value = data[f.name]
                if (value != null && value !is kotlinx.serialization.json.JsonNull && value !is JsonObject && value !is JsonArray) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(f.title, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(format(value, f.type))
                    }
                }
            }
        }
    }
}

@Composable
fun Countdown(w: WidgetSpec, state: ScreenState) {
    val data = state.results[w.operation] as? JsonObject
    val raw = w.field?.let { data?.get(it) } as? JsonPrimitive
    val until = raw?.content?.let { runCatching { OffsetDateTime.parse(it) }.getOrNull() }
    SectionCard(w.title) {
        if (until == null) Text("Нет активного периода") else {
            val left = Duration.between(OffsetDateTime.now(), until).toMinutes().coerceAtLeast(0)
            Text("до ${until.atZoneSameInstant(ZoneId.of("Europe/Moscow")).format(DateTimeFormatter.ofPattern("HH:mm"))}", style = MaterialTheme.typography.headlineMedium)
            Text("Осталось $left мин", color = if (left <= 10) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant)
            LinearProgressIndicator(progress = { (left.coerceAtMost(120) / 120f) }, modifier = Modifier.fillMaxWidth())
        }
    }
}

@Composable
fun ActionButton(w: WidgetSpec) {
    val op = AppSpec.operations[w.operation] ?: return
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var dialog by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }
    val values = remember { mutableStateMapOf<String, String>() }
    fun run() = scope.launch {
        try {
            val body = buildJsonObject {
                op.inputs.forEach { f ->
                    val v = values[f.name].orEmpty()
                    if (v.isNotBlank()) {
                        if (f.type in setOf("int", "decimal", "money")) put(f.name, v.toDoubleOrNull()) else put(f.name, v)
                    }
                }
            }
            val path = Api.resolvePath(op.path)
            val result = if (op.method == "GET") Api.request("GET", path) else Api.request(op.method, path, body)
            val url = (result as? JsonObject)?.values?.mapNotNull { (it as? JsonPrimitive)?.content }?.firstOrNull { it.startsWith("http") }
            if (url != null) context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
            message = "Выполнено"
        } catch (e: Exception) {
            message = e.message
        }
    }
    Column {
        Button(onClick = { if (op.inputs.isEmpty()) run() else dialog = true }, modifier = Modifier.fillMaxWidth()) { Text(w.title) }
        message?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
    }
    if (dialog) {
        AlertDialog(
            onDismissRequest = { dialog = false },
            title = { Text(op.title) },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    op.inputs.forEach { f ->
                        OutlinedTextField(values[f.name].orEmpty(), { values[f.name] = it }, label = { Text(f.title) }, singleLine = true)
                    }
                }
            },
            confirmButton = { TextButton(onClick = { dialog = false; run() }) { Text("Выполнить") } },
            dismissButton = { TextButton(onClick = { dialog = false }) { Text("Отмена") } },
        )
    }
}

@Composable
fun NotificationsWidget(w: WidgetSpec, state: ScreenState) {
    SectionCard(w.title) {
        val items = state.notifications.value
        if (items.isEmpty()) Text("Уведомлений нет", color = MaterialTheme.colorScheme.onSurfaceVariant)
        items.take(8).forEach { n ->
            Column(Modifier.padding(vertical = 4.dp)) {
                Text(n.str("body"))
                Text("${n.str("title")} · ${time(n.str("createdAt"))}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
fun ListWidget(w: WidgetSpec, state: ScreenState) {
    val e = w.entity?.let { AppSpec.entity(it) }
    val items = (state.results["list:${e?.name}"] as? JsonObject)?.get("items")?.jsonArray.orEmpty()
    SectionCard(w.title) {
        if (e == null || items.isEmpty()) Text("Записей нет", color = MaterialTheme.colorScheme.onSurfaceVariant)
        items.map { it.jsonObject }.forEach { row ->
            val labels = row["labels"] as? JsonObject
            Column(Modifier.padding(vertical = 4.dp)) {
                Text(row[e!!.labelField]?.let { format(it, "string") } ?: row.str("id").take(8), style = MaterialTheme.typography.bodyLarge)
                val details = e.fields.filter { it.name != e.labelField && it.type !in setOf("json", "geometry", "geo_point", "geo_line", "geo_polygon", "text") }
                    .take(3).mapNotNull { f ->
                        val v = labels?.get(f.name) ?: row[f.name] ?: return@mapNotNull null
                        if (v is kotlinx.serialization.json.JsonNull) null else "${f.title}: ${format(v, f.type, f.enumTitles)}"
                    }
                Text(details.joinToString(" · "), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
fun MapWidget(w: WidgetSpec, state: ScreenState) {
    val context = LocalContext.current
    val layers = w.layers.ifEmpty { AppSpec.entities.filter { it.geo }.map { it.name } }
    var collections by remember { mutableStateOf<Map<String, JsonObject>>(emptyMap()) }
    LaunchedEffect(Unit) {
        Configuration.getInstance().userAgentValue = context.packageName
        while (true) {
            try {
                val center = vehiclePosition(collections) ?: AppSpec.MAP_CENTER
                val bbox = "${center.second - 0.01},${center.first - 0.006},${center.second + 0.01},${center.first + 0.006}"
                collections = layers.mapNotNull { name ->
                    AppSpec.entity(name)?.let { e -> name to Api.request("GET", "/api/v1/${e.route}/geojson?bbox=$bbox").jsonObject }
                }.toMap()
            } catch (_: Exception) {
            }
            delay(4000)
        }
    }
    SectionCard(w.title) {
        AndroidView(
            factory = { ctx ->
                MapView(ctx).apply {
                    setTileSource(TileSourceFactory.MAPNIK)
                    setMultiTouchControls(true)
                    controller.setZoom(16.0)
                    controller.setCenter(GeoPoint(AppSpec.MAP_CENTER.first, AppSpec.MAP_CENTER.second))
                }
            },
            update = { map -> drawLayers(map, collections) },
            modifier = Modifier.fillMaxWidth().height(320.dp),
        )
    }
}

private fun vehiclePosition(collections: Map<String, JsonObject>): Pair<Double, Double>? =
    collections.values.flatMap { it["features"]?.jsonArray.orEmpty() }.map { it.jsonObject["geometry"]?.jsonObject }
        .firstOrNull { it?.str("type") == "Point" }?.let { g ->
            val c = g["coordinates"]!!.jsonArray
            Pair(c[1].jsonPrimitive.doubleOrNull ?: 0.0, c[0].jsonPrimitive.doubleOrNull ?: 0.0)
        }

private fun drawLayers(map: MapView, collections: Map<String, JsonObject>) {
    map.overlays.clear()
    val colors = listOf(0xFF1565C0.toInt(), 0xFF2E7D32.toInt(), 0xFFC62828.toInt())
    var focus: GeoPoint? = null
    collections.entries.forEachIndexed { index, (_, fc) ->
        fc["features"]?.jsonArray?.forEach { feature ->
            val f = feature.jsonObject
            val label = f["properties"]?.jsonObject?.str("label").orEmpty()
            val geometry = f["geometry"]?.jsonObject ?: return@forEach
            fun points(arr: JsonArray) = arr.map { p -> p.jsonArray.let { GeoPoint(it[1].jsonPrimitive.doubleOrNull ?: 0.0, it[0].jsonPrimitive.doubleOrNull ?: 0.0) } }
            when (geometry.str("type")) {
                "Point" -> {
                    val c = geometry["coordinates"]!!.jsonArray
                    val p = GeoPoint(c[1].jsonPrimitive.doubleOrNull ?: 0.0, c[0].jsonPrimitive.doubleOrNull ?: 0.0)
                    map.overlays.add(Marker(map).apply { position = p; title = label })
                    focus = p
                }
                "LineString" -> map.overlays.add(Polyline().apply { setPoints(points(geometry["coordinates"]!!.jsonArray)); outlinePaint.color = colors[index % 3]; outlinePaint.strokeWidth = 10f; title = label })
                "MultiLineString" -> geometry["coordinates"]!!.jsonArray.forEach { line ->
                    map.overlays.add(Polyline().apply { setPoints(points(line.jsonArray)); outlinePaint.color = colors[index % 3]; outlinePaint.strokeWidth = 10f; title = label })
                }
                "Polygon" -> map.overlays.add(Polygon().apply { points = points(geometry["coordinates"]!!.jsonArray[0].jsonArray); title = label })
            }
        }
    }
    focus?.let { map.controller.animateTo(it) }
    map.invalidate()
}

private fun JsonObject.str(key: String): String = (this[key] as? JsonPrimitive)?.content.orEmpty()

private fun format(value: JsonElement, type: String, enumTitles: Map<String, String> = emptyMap()): String {
    val p = value as? JsonPrimitive ?: return value.toString()
    if (!p.isString) return when (p.content) { "true" -> "Да"; "false" -> "Нет"; else -> p.content }
    return when {
        type == "datetime" || Regex("^\\d{4}-\\d{2}-\\d{2}T").containsMatchIn(p.content) -> time(p.content)
        enumTitles.containsKey(p.content) -> enumTitles.getValue(p.content)
        else -> p.content
    }
}

private fun time(iso: String): String = runCatching {
    OffsetDateTime.parse(iso).atZoneSameInstant(ZoneId.of("Europe/Moscow")).format(DateTimeFormatter.ofPattern("dd.MM HH:mm"))
}.getOrDefault(iso)

private fun notify(context: Context, title: String, body: String) {
    if (ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED &&
        android.os.Build.VERSION.SDK_INT >= 33) return
    val n = NotificationCompat.Builder(context, MainActivity.CHANNEL_ID)
        .setSmallIcon(android.R.drawable.ic_dialog_info)
        .setContentTitle(title)
        .setContentText(body)
        .setStyle(NotificationCompat.BigTextStyle().bigText(body))
        .setPriority(NotificationCompat.PRIORITY_HIGH)
        .build()
    NotificationManagerCompat.from(context).notify(body.hashCode(), n)
}
