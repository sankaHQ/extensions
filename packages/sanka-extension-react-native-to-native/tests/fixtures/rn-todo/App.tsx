import React from "react";
import { NavigationContainer } from "@react-navigation/native";
import { createNativeStackNavigator } from "@react-navigation/native-stack";
import HomeScreen from "./src/screens/HomeScreen";
import DetailScreen from "./src/screens/DetailScreen";

export type RootStackParamList = {
  Home: undefined;
  Detail: { id: number; title: string };
};

const Stack = createNativeStackNavigator<RootStackParamList>();

export default function App() {
  return (
    <NavigationContainer>
      <Stack.Navigator initialRouteName="Home">
        <Stack.Screen name="Home" component={HomeScreen} options={{ title: "Todos" }} />
        <Stack.Screen name="Detail" component={DetailScreen} options={{ title: "Todo" }} />
      </Stack.Navigator>
    </NavigationContainer>
  );
}
