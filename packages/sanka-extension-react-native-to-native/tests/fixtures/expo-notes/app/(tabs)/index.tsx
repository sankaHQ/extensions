import { useState } from "react";
import { FlatList, Pressable, StyleSheet, Text, View } from "react-native";
import { Link, useRouter } from "expo-router";
import { StatusBar } from "expo-status-bar";

type Note = { id: string; title: string };

export default function NotesScreen() {
  const router = useRouter();
  const [notes, setNotes] = useState<Note[]>([
    { id: "a", title: "First note" },
    { id: "b", title: "Second note" },
  ]);
  return (
    <View style={styles.container}>
      <StatusBar style="auto" />
      <FlatList
        data={notes}
        keyExtractor={(item) => item.id}
        renderItem={({ item }) => (
          <Pressable
            style={styles.row}
            onPress={() => router.push({ pathname: "/note/[id]", params: { id: item.id } })}
          >
            <Text style={styles.rowText}>{item.title}</Text>
          </Pressable>
        )}
      />
      {notes.length === 0 && <Text>No notes yet</Text>}
      <Link href="/settings" style={styles.link}>
        Open settings
      </Link>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, padding: 16 },
  row: { paddingVertical: 12, borderBottomWidth: 1, borderColor: "#eeeeee" },
  rowText: { fontSize: 16 },
  link: { marginTop: 16, color: "#0a84ff" },
});
