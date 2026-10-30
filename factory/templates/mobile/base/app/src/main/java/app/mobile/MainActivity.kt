package app.mobile

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Logout
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        createNotificationChannel()
        if (Build.VERSION.SDK_INT >= 33) {
            registerForActivityResult(ActivityResultContracts.RequestPermission()) {}.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        setContent { AppTheme { AppRoot() } }
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(CHANNEL_ID, "Уведомления", NotificationManager.IMPORTANCE_HIGH)
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).createNotificationChannel(channel)
    }

    companion object {
        const val CHANNEL_ID = "events"
    }
}

private val Scheme = lightColorScheme(primary = Color(0xFF1565C0), secondary = Color(0xFF2E7D32), tertiary = Color(0xFF6A1B9A))

@Composable
fun AppTheme(content: @Composable () -> Unit) = MaterialTheme(colorScheme = Scheme, content = content)

@Composable
fun AppRoot() {
    var loggedIn by remember { mutableStateOf(Api.token != null) }
    if (!loggedIn) LoginScreen { loggedIn = true } else HomeScreen { Api.logout(); loggedIn = false }
}

@Composable
fun LoginScreen(onLoggedIn: () -> Unit) {
    var login by remember { mutableStateOf(AppSpec.demoLogins.firstOrNull().orEmpty()) }
    var password by remember { mutableStateOf("") }
    var server by remember { mutableStateOf(Api.baseUrl) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    Column(
        Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp, Alignment.CenterVertically),
    ) {
        Text(AppSpec.PRODUCT, style = MaterialTheme.typography.headlineSmall, color = MaterialTheme.colorScheme.primary)
        Text("Вход в систему", style = MaterialTheme.typography.titleMedium)
        OutlinedTextField(login, { login = it }, label = { Text("Логин") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(
            password, { password = it }, label = { Text("Пароль") }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password), modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(server, { server = it }, label = { Text("Адрес сервера") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Button(
            onClick = {
                busy = true
                error = null
                scope.launch {
                    try {
                        Api.baseUrl = server
                        Api.login(login, password)
                        onLoggedIn()
                    } catch (e: Exception) {
                        error = e.message ?: "Не удалось войти"
                    } finally {
                        busy = false
                    }
                }
            },
            enabled = !busy, modifier = Modifier.fillMaxWidth(),
        ) { if (busy) CircularProgressIndicator(Modifier.padding(2.dp)) else Text("Войти") }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            AppSpec.demoLogins.forEach { AssistChip(onClick = { login = it }, label = { Text(it) }) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(onLogout: () -> Unit) {
    val screens = remember { AppSpec.screensFor(Api.role) }
    var selected by remember { mutableIntStateOf(0) }
    val screen = screens.getOrNull(selected)
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(screen?.title ?: AppSpec.PRODUCT) },
                actions = { IconButton(onClick = onLogout) { Icon(Icons.AutoMirrored.Filled.Logout, contentDescription = "Выйти") } },
            )
        },
        bottomBar = {
            if (screens.size > 1) NavigationBar {
                screens.forEachIndexed { i, s ->
                    NavigationBarItem(selected = i == selected, onClick = { selected = i }, icon = { Text("•") }, label = { Text(s.title) })
                }
            }
        },
    ) { padding ->
        if (screen == null) {
            Card(Modifier.padding(padding).padding(16.dp)) { Text("Для роли нет мобильных экранов", Modifier.padding(16.dp)) }
        } else {
            ScreenContent(screen, Modifier.padding(padding))
        }
    }
}
