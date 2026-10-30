package app.mobile

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.UUID
import java.util.concurrent.TimeUnit

class ApiException(val code: Int, message: String) : Exception(message)

/** Клиент REST API backend (JWT, ProblemDetails). */
object Api {
    var baseUrl: String = BuildConfig.API_URL
    var token: String? = null
    var userId: String? = null
    var role: String? = null
    var displayName: String = ""

    val json = Json { ignoreUnknownKeys = true; isLenient = true }
    private val client = OkHttpClient.Builder().callTimeout(20, TimeUnit.SECONDS).build()
    private val jsonType = "application/json".toMediaType()
    private val pathCache = mutableMapOf<String, String>()

    suspend fun request(method: String, path: String, body: JsonObject? = null): JsonElement = withContext(Dispatchers.IO) {
        val payload = (body ?: JsonObject(emptyMap())).toString().toRequestBody(jsonType)
        val builder = Request.Builder().url(baseUrl.trimEnd('/') + path)
        when (method) {
            "GET" -> builder.get()
            "DELETE" -> builder.delete()
            else -> builder.method(method, payload).header("Idempotency-Key", UUID.randomUUID().toString())
        }
        token?.let { builder.header("Authorization", "Bearer $it") }
        client.newCall(builder.build()).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw ApiException(response.code, problemTitle(text, response.code))
            if (text.isBlank()) JsonNull else json.parseToJsonElement(text)
        }
    }

    suspend fun login(login: String, password: String) {
        val result = request("POST", "/api/v1/auth/login", buildJsonObject {
            put("login", login)
            put("password", password)
        }).jsonObject
        token = result["token"]?.jsonPrimitive?.content
        val user = result["user"]?.jsonObject
        userId = user?.get("id")?.jsonPrimitive?.content
        role = user?.get("role")?.jsonPrimitive?.content
        displayName = user?.get("displayName")?.jsonPrimitive?.content.orEmpty()
        pathCache.clear()
    }

    fun logout() {
        token = null
        role = null
        pathCache.clear()
    }

    /** Подставляет в путь id записей, доступных пользователю: {vehicleId} → id первого «своего» автомобиля. */
    suspend fun resolvePath(template: String): String {
        var path = template
        Regex("\\{(\\w+)\\}").findAll(template).forEach { match ->
            val param = match.groupValues[1]
            val value = pathCache[param] ?: run {
                val entity = AppSpec.entityForParam(param) ?: return@forEach
                val list = request("GET", "/api/v1/${entity.route}?pageSize=1").jsonObject["items"]?.jsonArray
                list?.firstOrNull()?.jsonObject?.get("id")?.jsonPrimitive?.content?.also { pathCache[param] = it }
            } ?: return@forEach
            path = path.replace(match.value, value)
        }
        return path
    }

    private fun problemTitle(text: String, code: Int): String = try {
        val obj = json.parseToJsonElement(text).jsonObject
        val title = obj["title"]?.jsonPrimitive?.content ?: "Ошибка $code"
        val errors = obj["errors"]?.jsonObject?.values?.flatMap { v -> v.jsonArray.map { it.jsonPrimitive.content } }.orEmpty()
        (listOf(title) + errors).joinToString(": ")
    } catch (e: Exception) {
        if (code == 0) "Сервер недоступен" else "Ошибка $code"
    }
}
